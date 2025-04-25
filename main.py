
#!/usr/bin/env python3
"""
Main entry point script that runs the Flask application in app.py
"""

import subprocess
import sys

def main():
    print("Starting DocBot application...")
    try:
        # Run app.py with the same Python interpreter
        result = subprocess.run([sys.executable, "app.py"], check=True)
        return result.returncode
    except subprocess.CalledProcessError as e:
        print(f"Error running app.py: {e}")
        return e.returncode
    except KeyboardInterrupt:
        print("\nApplication stopped by user")
        return 0

if __name__ == "__main__":
    sys.exit(main())
