import os

SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "indian_stock_breakout_scanner.py")

def main():
    if not os.path.exists(SCRIPT_PATH):
        print(f"Error: {SCRIPT_PATH} not found.")
        return

    with open(SCRIPT_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    if "BACKGROUND_RUN = True" in content:
        new_content = content.replace("BACKGROUND_RUN = True", "BACKGROUND_RUN = False")
        status = "DISABLED"
    elif "BACKGROUND_RUN = False" in content:
        new_content = content.replace("BACKGROUND_RUN = False", "BACKGROUND_RUN = True")
        status = "ENABLED"
    else:
        print("Error: Could not find BACKGROUND_RUN configuration in the script.")
        return

    with open(SCRIPT_PATH, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"Background run is now {status}")

if __name__ == "__main__":
    main()
