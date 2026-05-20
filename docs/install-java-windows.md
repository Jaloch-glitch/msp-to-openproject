# Installing Java on Windows

This guide covers installing Java 21 (LTS) on Windows so the MSP Importer can start its JVM engine.

---

## Step 1 — Download the Installer

Open your browser and go to:

```
https://adoptium.net
```

On the download page, confirm these selections before clicking download:

| Setting          | Value              |
|------------------|--------------------|
| Version          | Temurin 21 (LTS)   |
| Operating System | Windows            |
| Architecture     | x64                |
| Package Type     | JDK                |

Click **Download .msi**

---

## Step 2 — Run the Installer

1. Open the downloaded `.msi` file from your Downloads folder
2. Click **Next** on the welcome screen
3. On the **Custom Setup** screen, verify these two options are set to **"Will be installed on local hard drive"**:

   - **Set JAVA_HOME variable**
   - **JavaSoft (Oracle) registry keys**

   Both are enabled by default — do not disable them.

4. Click **Next** then **Install**
5. Click **Finish** when complete

---

## Step 3 — Verify the Installation

Open a **new** Command Prompt window.

> Important: any Command Prompt or terminal that was open before the install must be closed and reopened. Old windows do not pick up new environment variables.

Press **Win + R**, type `cmd`, press **Enter**

Run the following command:

```cmd
java -version
```

Expected output:

```
openjdk version "21.0.3" 2024-04-16
OpenJDK Runtime Environment Temurin-21.0.3+9 (build 21.0.3+9)
OpenJDK 64-Bit Server VM Temurin-21.0.3+9 (build 21.0.3+9, mixed mode, sharing)
```

Also confirm JAVA_HOME was set correctly:

```cmd
echo %JAVA_HOME%
```

Expected output (path will vary slightly based on version):

```
C:\Program Files\Eclipse Adoptium\jdk-21.0.3.9-hotspot
```

---

## Step 4 — Run the Application

Close any open terminals, then double-click `start.bat` in the project folder.

The JVM error will no longer appear.

---

## Troubleshooting

### `echo %JAVA_HOME%` prints `%JAVA_HOME%` literally

The installer did not set the environment variable. Set it manually:

1. Press **Win + R**, type `sysdm.cpl`, press **Enter**
2. Click the **Advanced** tab
3. Click **Environment Variables**
4. Under **System variables**, click **New**

   | Field          | Value                                                          |
   |----------------|----------------------------------------------------------------|
   | Variable name  | `JAVA_HOME`                                                    |
   | Variable value | `C:\Program Files\Eclipse Adoptium\jdk-21.0.3.9-hotspot`      |

   Adjust the value to match your actual install path. To find it, open File Explorer and browse to `C:\Program Files\Eclipse Adoptium\` — the folder name is your path.

5. Find the **Path** variable under System variables, click **Edit**
6. Click **New** and add: `%JAVA_HOME%\bin`
7. Click **OK** on all open dialogs
8. Open a new Command Prompt and run `java -version` to confirm

---

### `java -version` still not found after setting JAVA_HOME

The `bin` folder was not added to PATH. Repeat steps 5 and 6 above.

---

### The installer asks to repair or remove an existing Java

An older version of Java is already installed. You can either:

- **Keep it** and install 21 alongside it — both can coexist
- **Remove it first** via Settings > Apps, search for "Java", uninstall, then re-run the Temurin installer

If multiple Java versions are installed, make sure `JAVA_HOME` points to the version you want to use.

---

### 32-bit vs 64-bit

If you downloaded the x64 installer but Windows shows an error about architecture mismatch, your Python installation may be 32-bit. Open a Command Prompt and run:

```cmd
python -c "import struct; print(struct.calcsize('P') * 8, 'bit')"
```

If it prints `32 bit`, download the **x86** Temurin installer instead. If it prints `64 bit`, the x64 installer is correct.
