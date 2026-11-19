"""Native phase filenames consumed by trace projection or supported client runners.

Keep renderer aliases and assessment bindings on one declaration. Runner-source
coverage tests guard outputs/continuation inputs that are not rendered directly.
"""

NATIVE_PROTOCOL_FILES = ("codex-native.jsonl", "native-ducc.jsonl")
NATIVE_STDERR_FILES = ("codex-native.stderr", "native-ducc.stderr", "runner.stderr")
NATIVE_RENDER_FILES = NATIVE_PROTOCOL_FILES + NATIVE_STDERR_FILES
NATIVE_CONTEXT_FILES = ("native-session.json", "conversation.json")
NATIVE_INVOCATION_FILES = (
    "native-invocation.json",
    "native-ducc-settings.json",
    "codex-native-argv.json",
    "fixture-native-argv.json",
)
NATIVE_FIXTURE_FILES = ("fixture-native.jsonl", "fixture-native.stderr")
NATIVE_PHASE_FILES = NATIVE_RENDER_FILES + NATIVE_CONTEXT_FILES + NATIVE_INVOCATION_FILES + NATIVE_FIXTURE_FILES
