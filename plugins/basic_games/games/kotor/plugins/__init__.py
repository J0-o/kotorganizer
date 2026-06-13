import mobase


def createPlugins() -> list[mobase.IPluginTool]:
    plugins = []
    try:
        from .builder_tool_plugin import KotorBuilderToolPlugin
    except ModuleNotFoundError as exc:
        if "builder" not in (exc.name or ""):
            raise
        KotorBuilderToolPlugin = None

    if KotorBuilderToolPlugin is not None:
        plugins.append(KotorBuilderToolPlugin())
    return plugins
