from uuid import uuid4

import pytest

from app.core.constants import ResponseStatus, UserRole
from app.services.crud_write_service import CrudWriteError, crud_write_service


class _DummySession:
    user_role = UserRole.NORMAL_USER.value


class _DummyDb:
    pass


def test_normal_user_cannot_confirm_pending_write(monkeypatch):
    def fake_get_active_session(db, session_id):
        return _DummySession()

    monkeypatch.setattr("app.services.crud_write_service.get_active_session", fake_get_active_session)

    with pytest.raises(CrudWriteError) as exc_info:
        crud_write_service.confirm_action(
            _DummyDb(),
            session_id=uuid4(),
            pending_action_id=uuid4(),
            actor_role=UserRole.NORMAL_USER,
            request_id="test-request",
        )

    assert exc_info.value.status == ResponseStatus.VALIDATION_FAILED
    assert exc_info.value.code == "admin_role_required_for_confirmation"
