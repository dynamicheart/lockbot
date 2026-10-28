"""The supported HTTP service starts without the removed Flask deployment stack."""

import os
import subprocess
import sys
import textwrap


def test_platform_startup_without_flask(tmp_path):
    environment = dict(os.environ)
    environment.update(
        DATA_DIR=str(tmp_path / "platform"),
        DATABASE_URL="sqlite:///" + str(tmp_path / "platform.db"),
        DEV_MODE="false",
        JWT_SECRET="platform-only-test-secret-at-least-32-bytes",
    )
    script = textwrap.dedent(
        """
        import importlib.abc
        import importlib.util
        import os
        import sys
        from cryptography.fernet import Fernet

        class NoFlask(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split('.')[0] == 'flask':
                    raise AssertionError('The platform still depends on Flask')

        sys.meta_path.insert(0, NoFlask())
        os.environ['ENCRYPTION_KEY'] = Fernet.generate_key().decode()
        assert importlib.util.find_spec('lockbot.core.entry') is None
        from fastapi.testclient import TestClient
        from lockbot.backend.app.main import create_app
        with TestClient(create_app()) as client:
            response = client.get('/api/health')
            assert response.status_code == 200
            assert response.json() == {'status': 'ok'}
        assert not any(name.split('.')[0] == 'flask' for name in sys.modules)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
