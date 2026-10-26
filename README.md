# robot-motion-mcp

A local stdio MCP server for inspecting robot motion-planning tasks and records, starting an isolated planning run, checking its status, and reporting validation evidence.

## Tools

| Tool | Purpose |
| --- | --- |
| `list_tasks` | List tasks found in the configured data roots and protocol fixtures. |
| `get_task_info` | Read task inputs, model/version information, completeness, and collision semantics. |
| `get_experiment_report` | Read a configured historical planning or validation record. |
| `start_motion_plan` | Start an offline planner in a per-run worktree and return a run ID. |
| `get_plan_status` | Read execution, planning-result, and validation state separately. |
| `validate_motion` | Report the available independent validation evidence. |

The server does not control a physical robot. A planner finding a path does not by itself mean that the path passed independent validation. A timeout is not proof that a path does not exist.

## Install

Requires Python 3.11 or newer.

```powershell
git clone <repository-url> robot-motion-mcp
cd robot-motion-mcp
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

## Configure local inputs

The repository contains the MCP server and Skill integration files. Planning inputs and the planner environment are configured on the machine that runs the server; they are not downloaded by the server automatically.

Set the roots to directories with the expected project layout. `ROBOT_MOTION_NEW_DESIGN_ROOT` points to a `New_Design` tree; `ROBOT_MOTION_AUDIT_ROOT` points to a directory of historical JSON records. For real planning, set `ROBOT_MOTION_PLANNER_PYTHON` to a Python interpreter that has the planner's required packages installed.

```powershell
$env:ROBOT_MOTION_NEW_DESIGN_ROOT = 'D:\robot-data\New_Design'
$env:ROBOT_MOTION_AUDIT_ROOT = 'D:\robot-data\research_audit'
$env:ROBOT_MOTION_RUNS_ROOT = 'D:\robot-data\robot-motion-runs'
$env:ROBOT_MOTION_PLANNER_PYTHON = 'D:\Python\envs\planner\python.exe'
```

The supported variables are `ROBOT_MOTION_WORKSPACE`, `ROBOT_MOTION_NEW_DESIGN_ROOT`, `ROBOT_MOTION_AUDIT_ROOT`, `ROBOT_MOTION_RUNS_ROOT`, and `ROBOT_MOTION_PLANNER_PYTHON`. Explicit root variables take precedence over the workspace defaults. The planner interpreter is fixed by configuration and cannot be selected by a model tool argument.

## Run

Start the server directly over stdio:

```powershell
.\.venv\Scripts\python.exe -m robot_motion_mcp.server
```

For an MCP host that loads workspace configuration, open `agent-workspace` as its working directory and use the included `.mcp.json`; `run_server.cmd` resolves the project and virtual environment relative to itself.

## Install the Skill

The companion repository, `robot-motion-diagnosis-skill`, contains the `robot-motion-diagnosis` Skill. Install it in the Skill directory used by your agent. The Skill expects the six tools listed above to be available.

## Development

```powershell
python -m pytest tests/test_runner.py -q
```

The included runner test uses a synthetic planner fixture. Tests that need a real planning workspace or a specific MCP host configuration should be run in an environment that provides those inputs.

