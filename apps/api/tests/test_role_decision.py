import pytest
from fastapi import HTTPException
from test_authorization import authorized as authorized
from test_authorization import create_item, database

from app.auth import actor_from_identity
from app.authorization import authorized_work_with_decision
from app.models import Role


@pytest.mark.parametrize("subject,organization_role,repository_role,effective_role", [
    ("admin", Role.ADMINISTRATOR, None, Role.ADMINISTRATOR),
    ("operator", Role.OPERATOR, None, Role.OPERATOR),
    ("user-123", Role.VIEWER, Role.APPROVER, Role.APPROVER),
])
async def test_decision_captures_the_roles_that_authorized_the_work(
    authorized, subject, organization_role, repository_role, effective_role,
):
    item = await create_item(authorized)
    async with database() as session:
        actor = await actor_from_identity(session, "https://identity.example", subject, "acme")
        work, decision = await authorized_work_with_decision(
            session, actor, item["id"], Role.OPERATOR, lock=True,
        )
        assert work.id == item["id"]
        assert decision.organization_role == organization_role
        assert decision.repository_role == repository_role
        assert decision.effective_role == effective_role
        assert decision.required_role == Role.OPERATOR


@pytest.mark.parametrize("subject,organization,status", [
    ("viewer", "acme", 403), ("admin", "other", 404),
])
async def test_denied_authorization_does_not_produce_a_decision(
    authorized, subject, organization, status,
):
    item = await create_item(authorized)
    async with database() as session:
        actor = await actor_from_identity(
            session, "https://identity.example", subject, organization,
        )
        with pytest.raises(HTTPException) as error:
            await authorized_work_with_decision(session, actor, item["id"], Role.OPERATOR)
        assert error.value.status_code == status
