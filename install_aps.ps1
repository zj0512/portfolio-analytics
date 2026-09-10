$py = "C:\Users\zc\AppData\Local\Programs\Python\Python312\python.exe"
& $py -m pip install apscheduler --quiet 2>&1 | Select-Object -Last 3
"=== verify ==="
& $py -c "import apscheduler; print('APScheduler', apscheduler.__version__)" 2>&1 | Select-Object -First 2
