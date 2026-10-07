"""Lazy provider composition for the independent roleplay adapter."""

from app.nvidia_roleplay import NVIDIANemotronRoleplayClient
from app.roleplay import RoleplayAdapter
from app.roleplay_client import JSONRoleplayAdapter


def get_roleplay_adapter() -> RoleplayAdapter:
    return JSONRoleplayAdapter(NVIDIANemotronRoleplayClient())
