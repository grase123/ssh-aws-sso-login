# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich>=13.0",
#     "typer>=0.12",
# ]
# ///
"""
SSH AWS SSO Login — a utility that performs aws sso login on a remote server,
automatically forwards the required port, and opens the browser for authentication.
"""

import re
import subprocess
import sys
import threading
import time
import webbrowser
from urllib.parse import unquote, urlparse, parse_qs

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt

__version__ = "0.1.0"

APP_NAME = "ssh-aws-sso-login"


def version_callback(value: bool) -> None:
    """Print version information in Linux style and exit."""
    if value:
        print(f"{APP_NAME} {__version__}")
        raise typer.Exit()


app = typer.Typer(
    name=APP_NAME,
    help="Perform aws sso login on a remote server via SSH, "
         "forward the callback port, and open the browser for authentication.",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


def fetch_remote_profiles(ssh_alias: str) -> list[str]:
    """Connect to the remote server and retrieve available AWS CLI profiles."""
    cmd = ["ssh", ssh_alias, "aws", "configure", "list-profiles"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            err_console.print(
                "[bold red]✗ Failed to fetch profiles from the remote server.[/bold red]"
            )
            if result.stderr.strip():
                err_console.print(f"  [dim]{result.stderr.strip()}[/dim]")
            raise typer.Exit(code=1)

        profiles = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not profiles:
            err_console.print("[bold red]✗ No AWS profiles found on the remote server.[/bold red]")
            raise typer.Exit(code=1)

        return profiles

    except subprocess.TimeoutExpired:
        err_console.print(
            "[bold red]✗ Timed out while fetching profiles from the remote server.[/bold red]"
        )
        raise typer.Exit(code=1)


def prompt_profile_selection(profiles: list[str]) -> str:
    """Display a numbered list of profiles and let the user pick one."""
    console.print("\n[bold cyan]Available AWS profiles on the remote server:[/bold cyan]\n")
    for i, name in enumerate(profiles, start=1):
        console.print(f"  [bold]{i:>3}[/bold]  {name}")
    console.print()

    choice = IntPrompt.ask(
        "[bold yellow]Select a profile number[/bold yellow]",
        choices=[str(i) for i in range(1, len(profiles) + 1)],
        console=console,
    )
    return profiles[choice - 1]


def extract_url_from_output(line: str) -> str | None:
    """Search for a URL in the output line of aws sso login."""
    match = re.search(r'https?://\S+', line)
    return match.group(0) if match else None


def parse_port_from_url(url: str) -> int:
    """URL-decode the link and extract the port from the redirect_uri parameter."""
    decoded_url = unquote(url)
    parsed = urlparse(decoded_url)
    params = parse_qs(parsed.query)

    redirect_uri_list = params.get("redirect_uri")
    if not redirect_uri_list:
        raise ValueError(f"'redirect_uri' parameter not found in URL: {decoded_url}")

    redirect_parsed = urlparse(redirect_uri_list[0])
    port = redirect_parsed.port
    if port is None:
        raise ValueError(f"Port not found in redirect_uri: {redirect_uri_list[0]}")

    return port


def run_sso_login(
    ssh_alias: str,
    profile_name: str,
    url_ready: threading.Event,
    login_done: threading.Event,
    login_error: threading.Event,
    shared: dict,
) -> None:
    """Thread1: run aws sso login on the remote server and parse the auth URL from stdout."""
    cmd = ["ssh", "-tt", ssh_alias, "aws", "sso", "login", "--profile", profile_name]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        shared["sso_process"] = proc

        for line in iter(proc.stdout.readline, ""):
            stripped = line.strip()
            if stripped:
                console.print(f"  [dim]\\[sso][/dim] {stripped}")

            if not url_ready.is_set():
                found_url = extract_url_from_output(stripped)
                if found_url:
                    shared["auth_url"] = found_url
                    try:
                        port = parse_port_from_url(found_url)
                        shared["port"] = port
                        url_ready.set()
                    except ValueError as e:
                        err_console.print(f"[red]URL parsing error:[/red] {e}")
                        login_error.set()
                        return

        proc.stdout.close()
        rc = proc.wait()

        if rc == 0:
            login_done.set()
        else:
            shared["sso_rc"] = rc
            login_error.set()

    except Exception as e:
        err_console.print(f"[red]Error in SSO login thread:[/red] {e}")
        login_error.set()


def run_port_forward(
    ssh_alias: str,
    port: int,
    tunnel_ready: threading.Event,
    shared: dict,
) -> None:
    """Thread2: forward the port from the remote server to localhost via SSH."""
    cmd = ["ssh", "-N", "-L", f"{port}:127.0.0.1:{port}", ssh_alias]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        shared["tunnel_process"] = proc
        tunnel_ready.set()
        proc.wait()
    except Exception as e:
        err_console.print(f"[red]Error in SSH tunnel thread:[/red] {e}")


def terminate_process(proc: subprocess.Popen | None) -> None:
    """Safely terminate a subprocess."""
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except Exception:
        pass


def wait_for_enter(enter_pressed: threading.Event) -> None:
    """Wait for the user to press Enter."""
    try:
        input()
        enter_pressed.set()
    except EOFError:
        pass


@app.command(
    help="Connect to a remote server via SSH, run aws sso login, "
         "forward the callback port, and open the browser for authentication."
)
def login(
    ssh_alias: str = typer.Argument(
        help="SSH alias (from ~/.ssh/config) used to connect to the remote server.",
    ),
    profile_name: str | None = typer.Argument(
        default=None,
        help="AWS CLI profile name for aws sso login. "
             "If omitted, you will be prompted to choose from the profiles available on the remote server.",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """Main command: start the SSO authentication process via SSH."""

    # If no profile given, fetch the list and let the user choose
    if profile_name is None:
        console.print(
            f"\n[bold yellow]▸[/bold yellow] No profile specified. "
            f"Fetching profiles from [bold]{ssh_alias}[/bold]…"
        )
        profiles = fetch_remote_profiles(ssh_alias)
        profile_name = prompt_profile_selection(profiles)

    console.print(Panel(
        f"[bold]SSH alias:[/bold]  {ssh_alias}\n"
        f"[bold]Profile:[/bold]    {profile_name}",
        title="[bold cyan]AWS SSO Login via SSH[/bold cyan]",
        border_style="cyan",
    ))

    # Synchronization primitives
    url_ready = threading.Event()
    login_done = threading.Event()
    login_error = threading.Event()
    tunnel_ready = threading.Event()
    enter_pressed = threading.Event()

    shared: dict = {}

    # Thread1: aws sso login
    console.print("\n[bold yellow]▸[/bold yellow] Starting aws sso login on the remote server…")
    t1 = threading.Thread(
        target=run_sso_login,
        args=(ssh_alias, profile_name, url_ready, login_done, login_error, shared),
        daemon=True,
    )
    t1.start()

    # Wait for the URL or an error
    while not url_ready.is_set() and not login_error.is_set():
        time.sleep(0.2)

    if login_error.is_set():
        err_console.print("[bold red]✗ Error during aws sso login.[/bold red]")
        terminate_process(shared.get("sso_process"))
        raise typer.Exit(code=1)

    port = shared["port"]
    auth_url = shared["auth_url"]

    console.print(f"[bold green]✓[/bold green] Authentication URL received")
    console.print(f"[bold green]✓[/bold green] Detected callback port: [bold]{port}[/bold]")

    # Thread2: SSH port forwarding
    console.print(f"[bold yellow]▸[/bold yellow] Starting SSH tunnel (port {port})…")
    t2 = threading.Thread(
        target=run_port_forward,
        args=(ssh_alias, port, tunnel_ready, shared),
        daemon=True,
    )
    t2.start()

    tunnel_ready.wait(timeout=10)
    if not tunnel_ready.is_set():
        err_console.print("[bold red]✗ Failed to start the SSH tunnel.[/bold red]")
        terminate_process(shared.get("sso_process"))
        raise typer.Exit(code=1)

    console.print(f"[bold green]✓[/bold green] SSH tunnel established")

    # Main thread: open the browser
    time.sleep(1)

    decoded_url = unquote(auth_url)
    console.print(f"\n[bold yellow]▸[/bold yellow] Opening the browser for authentication…")
    console.print(f"  [link={decoded_url}]{decoded_url}[/link]\n")
    webbrowser.open(decoded_url)

    # Wait for an event
    console.print(
        "[dim]Press[/dim] [bold]Enter[/bold] [dim]to abort, "
        "or wait for authentication to complete…[/dim]\n"
    )

    input_thread = threading.Thread(target=wait_for_enter, args=(enter_pressed,), daemon=True)
    input_thread.start()

    while True:
        # Event1: user pressed Enter — abort
        if enter_pressed.is_set():
            err_console.print("\n[bold yellow]⚠ Operation aborted by the user.[/bold yellow]")
            terminate_process(shared.get("sso_process"))
            terminate_process(shared.get("tunnel_process"))
            raise typer.Exit(code=1)

        # Event2: sso login completed successfully
        if login_done.is_set():
            console.print("[bold green]✓ Authentication completed successfully![/bold green]")
            terminate_process(shared.get("tunnel_process"))
            raise typer.Exit(code=0)

        # Thread1 finished with an error
        if login_error.is_set():
            rc = shared.get("sso_rc", "?")
            err_console.print(
                f"[bold red]✗ aws sso login failed (exit code {rc}).[/bold red]"
            )
            terminate_process(shared.get("tunnel_process"))
            raise typer.Exit(code=1)

        time.sleep(0.3)


if __name__ == "__main__":
    app()
