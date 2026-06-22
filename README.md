# SWR Analysis Tool v1.0.0 Release

A Windows dark-theme SWR sweep utility for CAT-controlled radios. This release was tested successfully with the Yaesu FT-710.
<img width="1122" height="672" alt="image" src="https://github.com/user-attachments/assets/37e1dbfb-b07a-4f72-862d-bdf031883306" />

## Run from source

```bat
pip install pyserial matplotlib
python swr_analysis_tool.py
```

## Build Windows EXE

Double-click:

```bat
build_exe.bat
```

The executable will be created at:

```bat
dist\SWR Analysis Tool.exe
```

## Release notes

- Stable release build based on the successful v0.4.4 test cycle.
- Amber segmented frequency display and CRT-style SWR graph.
- CAT connection watchdog and reconnect handling.
- Sweep safety validation and optional safety reminder suppression.
- Export CSV and image through the menu.
- SWR graph begins at 1.0 and scales to the configured abort SWR limit.
- Frequency entries accept comma or decimal separators, such as `14,200` or `14.200`.

Safety: This application keys the transmitter during sweeps. Use low power, verify antenna/dummy-load setup, and remain present at the radio.

Envisioned by N4EAC, Eduardo A. de Carvalho. Coded with AI.
