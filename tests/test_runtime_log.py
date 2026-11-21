import logging.config
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from foundation.runtime_log import BoundedLog


class RuntimeLogTests(unittest.TestCase):
    def test_stdout_sink_survives_server_logging_reconfiguration(self):
        with tempfile.TemporaryDirectory(prefix='stock-log-test-') as temp:
            path=Path(temp)/'service.log'
            stream=BoundedLog(path)
            with patch('sys.stderr',stream):
                logging.config.dictConfig({'version':1,'disable_existing_loggers':False,'handlers':{'console':{'class':'logging.StreamHandler','stream':'ext://sys.stderr'}},'loggers':{'stock-log-regression':{'handlers':['console'],'level':'INFO','propagate':False}}})
                logging.getLogger('stock-log-regression').info('server_started')
                stream.write('stdout_still_usable\n')
            stream.close()
            self.assertIn('server_started',path.read_text(encoding='utf-8'))
            self.assertIn('stdout_still_usable',path.read_text(encoding='utf-8'))
            logging.getLogger('stock-log-regression').handlers.clear()

    def test_log_rotation_bounds_real_files_and_preserves_recent_entries(self):
        with tempfile.TemporaryDirectory(prefix='stock-log-test-') as temp:
            path=Path(temp)/'service.log'
            stream=BoundedLog(path,max_bytes=100,backup_count=2)
            for i in range(8):stream.write(str(i)+':'+'x'*60+'\n')
            stream.close()
            files=list(Path(temp).glob('service.log*'))
            self.assertEqual(len(files),3)
            self.assertTrue(all(p.stat().st_size<=100 for p in files))
            self.assertTrue(path.read_text().startswith('7:'))
