"""Local version entrypoint with explicit deployment and worker ownership."""
import os
from pathlib import Path
import socket
import sys


def main():
    root = Path(__file__).resolve().parent
    os.chdir(root)
    from foundation import deployment
    profile = deployment.settings()
    port = int(profile['port'])
    os.environ.pop('STOCK_MYSQL_DATABASE', None)
    os.environ.pop('STOCK_UI_ACCEPTANCE', None)
    if '--managed' in sys.argv:
        if profile['workers_enabled']:
            os.environ.pop('STOCK_DISABLE_WORKER', None)
        else:
            os.environ['STOCK_DISABLE_WORKER'] = '1'
    runtime = root / 'data' / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    import msvcrt
    with (runtime / 'service.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0'); lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                raise RuntimeError(f'本机 {port} 端口已被占用，未启动第二个实例')
        if '--managed' in sys.argv:
            from foundation.runtime_log import BoundedLog
            sys.stdout = sys.stderr = BoundedLog(runtime / 'service.log')
        from foundation.mysql_runtime import ensure_running
        ensure_running()
        import uvicorn
        uvicorn.run('foundation.main:app', host='127.0.0.1', port=port, access_log=False)


if __name__ == '__main__':
    try:
        main()
    except BaseException as exc:
        if not isinstance(exc, SystemExit) or exc.code not in (None, 0):
            import traceback
            path = Path(__file__).resolve().parent / 'data/runtime/startup-error.log'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(traceback.format_exc()[-32768:], encoding='utf-8')
        raise
