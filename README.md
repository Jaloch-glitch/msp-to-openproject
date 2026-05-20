# MSP to OpenProject Importer

A tool for importing Microsoft Project files (.mpp, .mspdi, .mpx) into OpenProject via the REST API v3. Provides both a command-line interface and a browser-based web application with real-time progress streaming.

---

## Quick Start

**Requires Python 3.10+ and Java 8+ on your PATH.**

macOS / Linux:
```bash
./start.sh
```

Windows:
```cmd
start.bat
```

Both scripts create a virtual environment on first run, install all dependencies, and launch the web application at `http://localhost:8080`. Re-running the script on subsequent starts skips the install step if dependencies are already satisfied.

If you prefer a manual one-liner:

macOS / Linux:
```bash
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt && uvicorn web_app:app --host 0.0.0.0 --port 8080
```

Windows (Command Prompt):
```cmd
python -m venv venv && venv\Scripts\activate && pip install -r requirements.txt && uvicorn web_app:app --host 0.0.0.0 --port 8080
```

---

## What It Does

- Parses any MS Project file format using the mpxj Java library (via a Python-to-JVM bridge)
- Creates work packages in OpenProject preserving the full WBS hierarchy (parent-child relationships)
- Imports task dates, percentage complete, milestones, notes, WBS codes, baseline dates, costs, constraints, and resource assignments
- Creates task dependency relations (Finish-Start, Start-Start, Finish-Finish, Start-Finish) with lag support
- Exports any OpenProject project back to MSPDI format (openable in MS Project)
- Supports overwriting an existing project by clearing all its work packages before re-importing

---

## Architecture

```
msp_to_openproject.py   Core library: JVM bootstrap, MPP parsing, OPClient REST class, import/export logic
web_app.py              FastAPI application: upload endpoint, SSE log streaming, export endpoint
templates/index.html    Single-page frontend: file drop zone, real-time progress rings, live feed
requirements.txt        Python dependencies
```

The web app imports `msp_to_openproject` as a module and shares the `OPClient` instance. Each import job runs in a background thread. Log output is captured via a thread-scoped logging handler and streamed to the browser over Server-Sent Events (SSE).

---

## Prerequisites

### All platforms

- **Python 3.10 or later**
- **Java 8 or later** on your system PATH (JRE is sufficient; JDK is not required)
  - Verify with: `java -version`
- **Internet access** to reach your OpenProject instance

### Windows additional requirement

JPype1 requires a C++ compiler when no pre-built wheel exists for your Python version. Install the **Microsoft C++ Build Tools** before running `pip install`:

1. Download from [visualstudio.microsoft.com/visual-cpp-build-tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/)
2. Run the installer and select **Desktop development with C++**
3. Restart your terminal after installation

If a pre-built wheel is available for your Python version (check [pypi.org/project/JPype1](https://pypi.org/project/JPype1/#files)) no compiler is needed.

---

## Installation

### macOS / Linux

```bash
git clone <repository-url>
cd open_projects

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
```

### Windows

```cmd
git clone <repository-url>
cd open_projects

python -m venv venv
venv\Scripts\activate

pip install -r requirements.txt
```

If the JPype1 install fails with a compiler error, ensure the C++ Build Tools are installed (see Prerequisites above), then re-run the pip install command.

---

## Configuration

The OpenProject URL and API key are read from environment variables. Defaults are compiled into the code for convenience during development.

| Variable             | Default (dev)                               | Description                        |
|----------------------|---------------------------------------------|------------------------------------|
| `OPENPROJECT_URL`    | `https://openproject.burhaniengineers.com`  | Base URL of your OpenProject instance |
| `OPENPROJECT_API_KEY`| (see source)                                | API token from My Account > Access tokens |

Override at runtime:

**macOS / Linux:**
```bash
export OPENPROJECT_URL=https://your-instance.example.com
export OPENPROJECT_API_KEY=opapi-xxxxxxxxxxxx
```

**Windows (Command Prompt):**
```cmd
set OPENPROJECT_URL=https://your-instance.example.com
set OPENPROJECT_API_KEY=opapi-xxxxxxxxxxxx
```

**Windows (PowerShell):**
```powershell
$env:OPENPROJECT_URL = "https://your-instance.example.com"
$env:OPENPROJECT_API_KEY = "opapi-xxxxxxxxxxxx"
```

The API key must belong to an account with at least **Work package management** permission on the target project, and **Create project** permission if creating new projects.

---

## Running Locally

### Web Application (recommended)

**macOS / Linux:**
```bash
source venv/bin/activate
uvicorn web_app:app --host 0.0.0.0 --port 8080
```

**Windows:**
```cmd
venv\Scripts\activate
uvicorn web_app:app --host 0.0.0.0 --port 8080
```

Open `http://localhost:8080` in your browser.

Do not use `--reload` while an import is running. The file-watcher will restart the process and kill the in-progress import thread.

### Command-Line Interface

Import into an existing project (ID visible in the project URL):
```bash
python msp_to_openproject.py project.mpp --project-id 5
```

Create a new project and import into it:
```bash
python msp_to_openproject.py project.mpp --create-project "Site Renovation 2025"
```

Override connection details:
```bash
python msp_to_openproject.py project.mpp --project-id 5 \
    --url https://op.example.com --api-key opapi-xxxx
```

Enable debug output:
```bash
python msp_to_openproject.py project.mpp --project-id 5 -v
```

**Windows note:** Replace the backslash line continuation `\` with a caret `^`, or write the command on a single line.

---

## Web Application Usage

**Setup panel**

- Drop or browse for a `.mpp`, `.mspdi`, or `.mpx` file
- Choose an existing project from the dropdown, or enter a name to create a new one
- When an existing project is selected, two additional options appear:
  - **Export as MSPDI** — downloads the current OpenProject project as a `.xml` file that MS Project can open
  - **Overwrite existing content** — deletes all existing work packages in the project before importing (Phase 0 in the progress view)

**Import panel**

The progress view has two phases (three when overwriting):

- **Phase 0** (overwrite only) — deletion of existing work packages, shown with a red progress bar
- **Phase 1** — work package creation, shown with a blue ring and live activity feed
- **Phase 2** — dependency relation creation, shown with a purple ring

**Completion panel**

Displays final counts and a link to open the project directly in OpenProject.

---

## Deployment

### Choosing a server

The application is a standard ASGI app. For production, run it behind a reverse proxy rather than exposing uvicorn directly.

**Gunicorn with uvicorn workers** (Linux/macOS):
```bash
pip install gunicorn
gunicorn web_app:app -k uvicorn.workers.UvicornWorker -w 1 --bind 0.0.0.0:8080
```

Use exactly **one worker**. The JVM is a single in-process singleton; multiple workers would each start their own JVM in separate processes, which is valid, but stateful objects like `_jobs` and `_job_results` are not shared across processes. A load balancer with sticky sessions would be required for multi-worker setups.

### Reverse proxy (nginx example)

```nginx
server {
    listen 443 ssl;
    server_name importer.example.com;

    ssl_certificate     /etc/ssl/certs/your-cert.pem;
    ssl_certificate_key /etc/ssl/private/your-key.pem;

    client_max_body_size 100M;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "";
        proxy_set_header   Host $host;

        # Required for SSE (Server-Sent Events)
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 600s;
    }
}
```

`proxy_buffering off` is required for the live log stream to reach the browser without delay.

### Systemd service (Linux)

Create `/etc/systemd/system/msp-importer.service`:

```ini
[Unit]
Description=MSP to OpenProject Importer
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/msp-importer
Environment=OPENPROJECT_URL=https://your-instance.example.com
Environment=OPENPROJECT_API_KEY=opapi-xxxxxxxxxxxx
ExecStart=/opt/msp-importer/venv/bin/uvicorn web_app:app --host 127.0.0.1 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable msp-importer
systemctl start msp-importer
```

### Windows Service

Use [NSSM (Non-Sucking Service Manager)](https://nssm.cc) to wrap the uvicorn process as a Windows service:

```cmd
nssm install msp-importer "C:\msp-importer\venv\Scripts\uvicorn.exe"
nssm set msp-importer AppParameters "web_app:app --host 127.0.0.1 --port 8080"
nssm set msp-importer AppDirectory "C:\msp-importer"
nssm set msp-importer AppEnvironmentExtra OPENPROJECT_URL=https://your-instance.example.com
nssm set msp-importer AppEnvironmentExtra OPENPROJECT_API_KEY=opapi-xxxxxxxxxxxx
nssm start msp-importer
```

### Docker

```dockerfile
FROM eclipse-temurin:21-jre-jammy

RUN apt-get update && apt-get install -y python3 python3-pip python3-venv && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv venv && ./venv/bin/pip install --no-cache-dir -r requirements.txt

COPY . .

ENV OPENPROJECT_URL=""
ENV OPENPROJECT_API_KEY=""

EXPOSE 8080
CMD ["./venv/bin/uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "8080"]
```

```bash
docker build -t msp-importer .
docker run -p 8080:8080 \
  -e OPENPROJECT_URL=https://your-instance.example.com \
  -e OPENPROJECT_API_KEY=opapi-xxxxxxxxxxxx \
  msp-importer
```

### Production considerations

**JVM startup:** The JVM starts on the first import request and stays alive for the process lifetime. The first import after a cold start will take an extra 2-5 seconds for JVM initialisation. Subsequent imports start immediately.

**File uploads:** Uploaded files are written to the OS temp directory and deleted when the import finishes (success or failure). Ensure the temp directory has sufficient space for large `.mpp` files.

**Memory:** The JVM heap defaults to a quarter of system RAM. For very large projects (thousands of tasks), increase the heap by setting `_JAVA_OPTIONS=-Xmx2g` in the service environment.

**API key permissions:** The API key used must belong to an account with administrator rights if you want to list and assign users. Without admin rights, user assignment is silently skipped — tasks will import without assignees.

**Concurrent imports:** The application supports multiple simultaneous import jobs. Each job gets its own log queue and thread. The OpenProject API is the bottleneck; concurrent jobs will share the HTTP connection pool and may slow each other down on large projects.

**HTTPS:** Always use HTTPS in production. The API key is sent as an HTTP Basic Auth password on every request. Exposing the application over plain HTTP leaks the key.

**API key in source code:** The default key is compiled into the source. For production deployments, always override it via environment variables and ensure the source repository is private, or strip the default key before committing.

---

## Troubleshooting

See `docs/reference.md` for a complete debugging reference including log patterns, API error codes, known issues, and component internals.

Common issues:

- **`java -version` not found** — Java is not on PATH. Install Java 8+ and ensure it is in your system PATH.
- **`pip install JPype1` fails on Windows** — Install Microsoft C++ Build Tools (see Prerequisites).
- **Connection refused on health check** — The server is not running, or is bound to a different port.
- **401 on project list** — API key is invalid or has expired.
- **422 on project creation** — The identifier (URL slug) is already taken. Choose a different one.
