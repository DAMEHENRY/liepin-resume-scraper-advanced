# Build Instructions

This guide explains how to package the `LiepinScraper` script into a standalone executable for macOS and Windows.

## Prerequisites

Ensure you have the dependencies installed:
```bash
pip install pyinstaller rich playwright pandas openpyxl python-docx beautifulsoup4 requests pypinyin htmldocx
```

## 1. Building for macOS

1.  Open a terminal in the project directory.
2.  Run the build script:
    ```bash
    sh build_mac.sh
    ```
3.  The executable will be created at `dist/LiepinScraper`.
4.  You can zip this file and share it. Users just need to unzip and double-click (or run from terminal).

> **Note**: On first run, the app will check for and install the Chromium browser if missing.

## 2. Building for Windows

**Important**: You must perform these steps on a **Windows** machine.

1.  Copy the entire project folder to your Windows machine.
2.  Open Command Prompt or PowerShell in the project folder.
3.  Install Python and dependencies (same as above).
4.  Run the following command:
    ```cmd
    pyinstaller --noconfirm --onefile --console --name "LiepinScraper" --add-data "libs;libs" --hidden-import "rich" main.py
    ```
    *(Note the semicolon `;` in `--add-data "libs;libs"` which is specific to Windows)*
5.  The `LiepinScraper.exe` will be found in the `dist` folder.

## Troubleshooting

*   **"Module not found"**: If the app crashes saying a module is missing, add `--hidden-import "module_name"` to the PyInstaller command.
*   **Browser issues**: Ensure the user has internet access on the first run so Playwright can download the browser.
