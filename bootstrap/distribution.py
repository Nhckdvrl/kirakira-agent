"""The shipped application selects ordinary owners; the host has no business list."""

from agent.composition import Module
from bus import provider as transport
from plugins.channels import provider as channel_provider
from plugins.context import provider as context_provider
from plugins.control import provider as control_provider
from plugins.conversation import provider as conversation_provider
from plugins.dashboard import provider as dashboard_provider
from plugins.default_memory import provider as memory_provider
from plugins.delivery import provider as delivery_provider
from plugins.execution import provider as execution_provider
from plugins.extensions import provider as extensions
from plugins.ledger import provider as ledger_provider
from plugins.jobs import provider as jobs_provider
from plugins.mcp import provider as mcp_provider
from plugins.models import provider as model_provider
from plugins.proactive_flow import provider as proactive_provider
from plugins.projects import provider as project_provider
from plugins.react import provider as react_provider
from plugins.reply import provider as reply_provider
from plugins.reply_program import default_provider
from plugins.reply_program import provider as program_provider
from plugins.runtime_restart import provider as restart_provider
from plugins.scheduler import provider as scheduler_provider
from plugins.sources import provider as source_provider
from plugins.subagent import provider as subagent_provider
from plugins.tools import provider as tool_provider

PROVIDERS = (
    ("transport", transport),
    ("ledger", ledger_provider),
    ("jobs", jobs_provider),
    ("projects", project_provider),
    ("models", model_provider),
    ("memory", memory_provider),
    ("tools", tool_provider),
    ("mcp", mcp_provider),
    ("context", context_provider),
    ("execution", execution_provider),
    ("sources", source_provider),
    ("reply_program", program_provider),
    ("extensions", extensions),
    ("react", react_provider),
    ("subagent", subagent_provider),
    ("delivery", delivery_provider),
    ("default_reply", default_provider),
    ("conversation", conversation_provider),
    ("scheduler", scheduler_provider),
    ("reply", reply_provider),
    ("proactive", proactive_provider),
    ("restart", restart_provider),
    ("control", control_provider),
    ("dashboard", dashboard_provider),
    ("channels", channel_provider),
)


def modules(workspace, app_config, channel_flags):
    config = {
        "workspace": workspace,
        "app_config": app_config,
        "channels": channel_flags,
    }
    return tuple(
        Module(name, owner.apply, owner.inject, owner.provides, config, native=True)
        for name, owner in PROVIDERS
    )


def application_fields(root):
    from bus.contract import BUS, EVENTS
    from plugins.channels.contract import CHANNELS
    from plugins.context.contract import CONTEXT
    from plugins.control.contract import CONTROL
    from plugins.conversation.contract import CONVERSATION
    from plugins.default_memory.contract import MEMORY, MEMORY_SERVICES
    from plugins.extensions.contract import EXTENSION_WATCHER, EXTENSIONS
    from plugins.ledger.contract import SESSIONS
    from plugins.mcp.contract import MCP_WATCHER
    from plugins.proactive_flow.contract import DRIFT, PROACTIVE
    from plugins.react.contract import REACT
    from plugins.reply.contract import FOLLOWER
    from plugins.runtime_restart.contract import RESTART
    from plugins.scheduler.contract import SCHEDULER
    from plugins.subagent.contract import SUBAGENTS
    from plugins.tools.contract import TOOLS

    contracts = {
        "bus": BUS,
        "event_bus": EVENTS,
        "session_manager": SESSIONS,
        "memory": MEMORY,
        "memory_services": MEMORY_SERVICES,
        "tools": TOOLS,
        "context": CONTEXT,
        "reasoner": REACT,
        "pipeline": CONVERSATION,
        "loop": FOLLOWER,
        "channel_host": CHANNELS,
        "plugin_manager": EXTENSIONS,
        "plugin_watcher": EXTENSION_WATCHER,
        "mcp_watcher": MCP_WATCHER,
        "scheduler": SCHEDULER,
        "subagents": SUBAGENTS,
        "proactive_loop": PROACTIVE,
        "drift_runner": DRIFT,
    }
    values = {name: root.services.resolve(key) for name, key in contracts.items()}
    control = root.services.resolve(CONTROL)
    values.update(
        zip(
            ("control_store", "control_runtime", "control_service", "control_server"),
            control,
        )
    )
    values["restart_coordinator"] = root.services.resolve(RESTART)[1]
    values["_service_fields"] = {
        **{name: (key, None) for name, key in contracts.items()},
        **{
            name: (CONTROL, index)
            for index, name in enumerate(
                (
                    "control_store",
                    "control_runtime",
                    "control_service",
                    "control_server",
                )
            )
        },
        "restart_coordinator": (RESTART, 1),
    }
    return values
