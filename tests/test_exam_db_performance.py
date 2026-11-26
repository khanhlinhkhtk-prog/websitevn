import unittest
import os
from unittest.mock import MagicMock, patch
from psycopg2 import OperationalError

os.environ.setdefault('GOOGLE_API_KEY', 'test-key')
import app as website


class ExamDatabaseTests(unittest.TestCase):
    def test_login_database_outage_shows_service_error(self):
        login_paths = (
            '/exam_system/student_login',
            '/exam_system/teacher_login',
            '/exam_system/parent_login',
        )
        with patch.object(website, 'DATABASE_URL', 'postgresql://unavailable/db'), \
                patch.object(website, '_exam_db_pool', None), \
                patch.object(website, '_exam_store_initialized', False), \
                patch('psycopg2.pool.ThreadedConnectionPool',
                      side_effect=OperationalError('could not resolve host')):
            client = website.app.test_client()
            for path in login_paths:
                with self.subTest(path=path):
                    response = client.post(path, data={
                        'username': 'qa_nonexistent',
                        'password': 'qa_invalid',
                    })
                    self.assertEqual(response.status_code, 503)
                    self.assertIn('Không thể kết nối dữ liệu đăng nhập',
                                  response.get_data(as_text=True))

    def test_request_reuses_connection_and_returns_it_to_pool(self):
        pool = MagicMock()
        conn = pool.getconn.return_value
        conn.closed = False
        with patch.object(website, '_exam_db_pool', pool):
            with website.app.test_request_context('/'):
                self.assertIs(website.get_exam_db_connection(), conn)
                self.assertIs(website.get_exam_db_connection(), conn)
                pool.getconn.assert_called_once()
            pool.putconn.assert_called_once_with(conn, close=False)

    def test_connection_returned_after_request_exception(self):
        pool = MagicMock()
        pool.getconn.return_value.closed = False
        with patch.object(website, '_exam_db_pool', pool):
            with self.assertRaises(RuntimeError):
                with website.app.test_request_context('/'):
                    website.get_exam_db_connection()
                    raise RuntimeError('request failed')
            pool.putconn.assert_called_once()

    def test_non_request_session_returns_connection_on_error(self):
        pool = MagicMock()
        pool.getconn.return_value.closed = False
        with patch.object(website, '_exam_db_pool', pool):
            with self.assertRaises(RuntimeError):
                with website.exam_db_session():
                    raise RuntimeError('write failed')
            pool.getconn.return_value.__exit__.assert_called_once()
            pool.putconn.assert_called_once()

    def test_login_only_reads_users_without_bulk_preload(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ({'teachers': []},)
        with website.app.test_request_context('/exam_system/teacher_login'):
            with patch.object(website, 'DATABASE_URL', 'configured'), \
                    patch.object(website, '_exam_store_initialized', True), \
                    patch.object(website, 'get_exam_db_connection', return_value=conn), \
                    patch.object(website, 'preload_exam_collections') as preload:
                self.assertEqual(website.load_exam_users()['teachers'], [])
                preload.assert_not_called()
                cursor.execute.assert_called_once_with(
                    'SELECT payload FROM exam_system_store WHERE collection = %s',
                    ('users',),
                )
                conn.__enter__.assert_not_called()


if __name__ == '__main__':
    unittest.main()
