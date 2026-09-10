@echo off
set MSI=D:\LENOVO\Tailscale\tailscale-setup-amd64.msi
set TARGET=D:\Tailscale
echo Starting Tailscale Setup Wizard targeting D:\Tailscale ...
start msiexec.exe /i "%MSI%" INSTALLDIR="%TARGET%"
