# ssh_aws_sso_login

A CLI utility that performs `aws sso login` on a remote server via SSH, automatically
forwards the authentication callback port to localhost, and opens the default browser
for the user to complete the SSO authentication flow.

## Problem

When AWS CLI is configured with SSO on a remote (headless) server, `aws sso login`
starts a local HTTP listener on a random port and prints a browser URL containing a
`redirect_uri` that points to `http://127.0.0.1:<port>/...`. Since there is no browser
on the server and the port is not accessible from the local machine, the authentication
flow cannot be completed. This script solves the problem by:

1. Parsing the authentication URL and extracting the callback port.
2. Forwarding that port from the remote server to `localhost` via an SSH tunnel.
3. Opening the URL in the local browser so the redirect lands on the forwarded port.

## Requirements

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/) package manager
- SSH client with a configured alias in `~/.ssh/config`
- AWS CLI v2 installed and configured with SSO profiles on the remote server

## Installation & Running

Currently the script is designed to be run directly via `uv run`:

```bash
uv run ssh_aws_sso_login.py <ssh_alias> [profile_name]
```

`uv` automatically installs the dependencies declared in the inline script metadata
header on the first run.

## Version

The version is defined as a global variable `__version__` at the top of the script.
Current version: **0.1.0**

To check the version:

```bash
uv run ssh_aws_sso_login.py --version
uv run ssh_aws_sso_login.py -V
```

Output follows the standard Linux convention:

```
ssh-aws-sso-login 0.1.0
```

## Dependencies

Declared in the [PEP 723](https://peps.python.org/pep-0723/) inline metadata block:

| Package | Version  | Purpose                                   |
|---------|----------|-------------------------------------------|
| rich    | >= 13.0  | Colored and formatted terminal output     |
| typer   | >= 0.12  | CLI framework with auto-generated help    |

## Input

| Argument       | Type           | Required | Description                                                                                                       |
|----------------|----------------|----------|-------------------------------------------------------------------------------------------------------------------|
| `ssh_alias`    | `str`          | Yes      | SSH alias from `~/.ssh/config` used to connect to the remote server.                                              |
| `profile_name` | `str \| None`  | No       | AWS CLI profile name. If omitted, the script fetches available profiles from the remote server and prompts the user to choose one. |

| Option              | Short | Description                |
|---------------------|-------|----------------------------|
| `--version`         | `-V`  | Show version and exit.     |
| `--help`            |       | Show help message and exit.|

## Output

- **Exit code 0** — authentication completed successfully.
- **Exit code 1** — an error occurred or the user aborted the operation.
- All status messages are printed to stdout using `rich` formatting.
- Error messages are printed to stderr.

## Algorithm

The script operates with three concurrent execution contexts coordinated via
`threading.Event` objects and a shared dictionary.

### Step 0 — Profile Resolution (optional)

If `profile_name` is not provided:

1. Connect to the remote server via SSH and run `aws configure list-profiles`.
2. Parse stdout into a list of profile names.
3. Display a numbered list and prompt the user to select one using `rich.prompt.IntPrompt`.

### Step 1 — Thread 1: SSO Login

1. Spawn `ssh -tt <alias> aws sso login --profile <profile>` as a subprocess.
2. Read stdout line by line in real-time (line-buffered).
3. Search each line for a URL matching `https?://\S+`.
4. When found, URL-decode the link, parse the query string, extract the `redirect_uri`
   parameter, and read the port number from it.
5. Signal `url_ready` event with the port and URL stored in the shared dict.
6. Continue reading stdout until the process exits.
7. On exit code 0 → signal `login_done`; otherwise → signal `login_error`.

### Step 2 — Thread 2: SSH Port Forwarding

Started after `url_ready` is signaled:

1. Spawn `ssh -N -L <port>:127.0.0.1:<port> <alias>` as a subprocess.
2. Signal `tunnel_ready` immediately after the process starts.
3. Keep the tunnel open until terminated.

### Step 3 — Main Thread: Browser & Event Loop

1. Wait for `tunnel_ready` (timeout: 10 seconds).
2. Sleep 1 second to allow the tunnel to stabilize.
3. Open the decoded authentication URL in the default browser via `webbrowser.open()`.
4. Start a background thread listening for `Enter` keypress.
5. Enter an event loop polling every 300ms for:
   - **Event 1 — User presses Enter**: terminate all subprocesses, exit with code 1,
     print "Operation aborted by the user."
   - **Event 2 — `login_done` signaled**: terminate the tunnel, exit with code 0,
     print "Authentication completed successfully!"
   - **Event 3 — `login_error` signaled**: terminate the tunnel, exit with code 1,
     print the error with the exit code.

### Synchronization

| Event          | Set by    | Purpose                                      |
|----------------|-----------|----------------------------------------------|
| `url_ready`    | Thread 1  | Auth URL parsed and port extracted            |
| `login_done`   | Thread 1  | `aws sso login` exited with code 0           |
| `login_error`  | Thread 1  | `aws sso login` exited with non-zero code    |
| `tunnel_ready` | Thread 2  | SSH tunnel subprocess started                 |
| `enter_pressed`| Input thr.| User pressed Enter to abort                  |

### Process Cleanup

`terminate_process()` sends `SIGTERM` first and waits up to 5 seconds; if the process
does not exit, it sends `SIGKILL`. All threads are daemonized, so they are automatically
cleaned up when the main thread exits.

## Implementation Details

### Key Functions

| Function                    | Description                                                         |
|-----------------------------|---------------------------------------------------------------------|
| `version_callback()`       | Prints version in Linux style (`<name> <version>`) and exits        |
| `fetch_remote_profiles()`   | Runs `aws configure list-profiles` via SSH, returns a list of names |
| `prompt_profile_selection()`| Displays a numbered list and prompts for selection via `IntPrompt`  |
| `extract_url_from_output()` | Regex search for `https?://\S+` in a line of text                  |
| `parse_port_from_url()`     | URL-decodes the link, parses `redirect_uri`, extracts the port      |
| `run_sso_login()`           | Thread 1 target — manages the SSO login subprocess                  |
| `run_port_forward()`        | Thread 2 target — manages the SSH tunnel subprocess                 |
| `terminate_process()`       | Graceful SIGTERM → SIGKILL subprocess termination                   |
| `wait_for_enter()`          | Blocking `input()` in a daemon thread for abort detection           |

### Global Variables

| Variable        | Value              | Description                        |
|-----------------|--------------------|------------------------------------|
| `__version__`   | `"0.1.0"`          | Semantic version of the script     |
| `APP_NAME`      | `"ssh-aws-sso-login"` | Application name used in CLI and version output |

### SSH Flags Used

| Flag  | Command          | Purpose                                          |
|-------|------------------|--------------------------------------------------|
| `-tt` | `ssh -tt` (login)| Force pseudo-terminal allocation for interactive commands |
| `-N`  | `ssh -N` (tunnel)| Do not execute a remote command (tunnel only)    |
| `-L`  | `ssh -L` (tunnel)| Local port forwarding `local:remote`             |

## Usage Examples

```bash
# With explicit profile
uv run ssh_aws_sso_login.py my-server production

# Interactive profile selection
uv run ssh_aws_sso_login.py my-server

# Show version
uv run ssh_aws_sso_login.py --version

# Show help
uv run ssh_aws_sso_login.py --help
```

## TODO

### Make the Script Installable via `uv tool install`

Currently the script uses [PEP 723](https://peps.python.org/pep-0723/) inline metadata
and is executed directly with `uv run`. To make it installable as a global tool via
`uv tool install`, the following changes are needed:

1. **Create a proper Python package structure:**
   ```
   ssh-aws-sso-login/
   ├── pyproject.toml
   └── src/
       └── ssh_aws_sso_login/
           ├── __init__.py          # define __version__ here
           └── cli.py               # move all code here
   ```

2. **Create `pyproject.toml`** with `[build-system]`, `[project]`, and
   `[project.scripts]` sections:
   ```toml
   [build-system]
   requires = ["hatchling"]
   build-backend = "hatchling.build"

   [project]
   name = "ssh-aws-sso-login"
   version = "0.1.0"
   description = "Perform aws sso login on a remote server via SSH with automatic port forwarding"
   requires-python = ">=3.12"
   dependencies = [
       "rich>=13.0",
       "typer>=0.12",
   ]

   [project.scripts]
   ssh-aws-sso-login = "ssh_aws_sso_login.cli:app"
   ```

3. **Move the Typer app** from the standalone script into `src/ssh_aws_sso_login/cli.py`.
   Remove the `if __name__ == "__main__"` block (the entry point is handled by
   `[project.scripts]`). Remove the PEP 723 inline metadata header.

4. **Install globally** with:
   ```bash
   # From local directory
   uv tool install ./ssh-aws-sso-login

   # Or from a git repository
   uv tool install git+https://github.com/<user>/ssh-aws-sso-login.git
   ```

5. After installation, the tool is available system-wide as:
   ```bash
   ssh-aws-sso-login my-server production
   ```
