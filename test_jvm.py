#!/usr/bin/env python3
"""
Quick JVM / Java installation check.

Run this before starting the web app to confirm Java and mpxj are working:

    python test_jvm.py          (macOS / Linux)
    python test_jvm.py          (Windows — run from inside the venv)

Prints a pass/fail report for each requirement.
"""

import sys

PASS = "  PASS"
FAIL = "  FAIL"
INFO = "  INFO"

def line(status, label, detail=""):
    detail_str = f"  ->  {detail}" if detail else ""
    print(f"{status}  {label}{detail_str}")

print()
print("JVM Installation Check")
print("=" * 52)

all_ok = True

# ── 1. Python version ─────────────────────────────────
py = sys.version_info
if py >= (3, 10):
    line(PASS, "Python version", f"{py.major}.{py.minor}.{py.micro}")
else:
    line(FAIL, "Python version", f"{py.major}.{py.minor}.{py.micro} — need 3.10+")
    all_ok = False

# ── 2. JPype1 installed ───────────────────────────────
try:
    import jpype
    import jpype.imports
    line(PASS, "JPype1 installed", jpype.__version__)
except ImportError:
    line(FAIL, "JPype1 installed", "not found — run: pip install JPype1")
    all_ok = False
    print()
    print("Cannot continue without JPype1.")
    sys.exit(1)

# ── 3. mpxj installed ─────────────────────────────────
try:
    import mpxj as _mpxj
    line(PASS, "mpxj installed", f"JARs at {_mpxj.mpxj_dir}")
except ImportError:
    line(FAIL, "mpxj installed", "not found — run: pip install mpxj")
    all_ok = False
    print()
    print("Cannot continue without mpxj.")
    sys.exit(1)

# ── 4. JVM path resolvable ────────────────────────────
try:
    jvm_path = jpype.getDefaultJVMPath()
    line(PASS, "JVM path found", jvm_path)
except Exception as e:
    line(FAIL, "JVM path found", str(e))
    all_ok = False
    if sys.platform == "win32":
        print()
        print("  Java is not installed or JAVA_HOME is not set.")
        print("  See docs/install-java-windows.md for step-by-step instructions.")
    else:
        print()
        print("  Install Java 11+ and ensure it is on your PATH.")
        print("  Verify with: java -version")
    print()
    sys.exit(1)

# ── 5. JVM starts ─────────────────────────────────────
import glob, os
if not jpype.isJVMStarted():
    try:
        for jar in glob.glob(os.path.join(_mpxj.mpxj_dir, "*.jar")):
            jpype.addClassPath(jar)
        jpype.startJVM(jvm_path, convertStrings=True)
        line(PASS, "JVM started")
    except Exception as e:
        line(FAIL, "JVM started", str(e))
        all_ok = False
        print()
        sys.exit(1)
else:
    line(INFO, "JVM started", "already running")

# ── 6. Java version ───────────────────────────────────
try:
    from jpype import JClass
    System = JClass("java.lang.System")
    java_ver = str(System.getProperty("java.version"))
    java_home = str(System.getProperty("java.home"))
    line(PASS, "Java version", java_ver)
    line(INFO, "Java home", java_home)
except Exception as e:
    line(FAIL, "Java version", str(e))
    all_ok = False

# ── 7. mpxj readable ─────────────────────────────────
try:
    UPR = JClass("org.mpxj.reader.UniversalProjectReader")
    line(PASS, "mpxj UniversalProjectReader loaded")
except Exception as e:
    line(FAIL, "mpxj UniversalProjectReader loaded", str(e))
    all_ok = False

# ── Summary ───────────────────────────────────────────
print()
print("=" * 52)
if all_ok:
    print("  All checks passed. The application is ready to run.")
else:
    print("  One or more checks failed. Fix the issues above,")
    print("  then re-run this script to confirm.")
print()
