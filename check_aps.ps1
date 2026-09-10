$py = "C:\Users\zc\AppData\Local\Programs\Python\Python312\python.exe"
"=== 检查 APScheduler ==="
& $py -c "import apscheduler; print('APScheduler', apscheduler.__version__)" 2>&1 | Select-Object -First 2
