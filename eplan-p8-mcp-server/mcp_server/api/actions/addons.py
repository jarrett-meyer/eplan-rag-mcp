"""
API modules and add-on actions.
"""

from ._base import _get_connected_manager, _build_action


def load_api_module(module_path: str) -> dict:
    """
    Load and register an API add-in.
    Action: EplApiModuleAction

    Args:
        module_path: File name of the Add-in DLL to register (parameter register).
                     If no absolute path is given, it is resolved against the
                     current directory.
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    action = _build_action(
        "EplApiModuleAction",
        register=module_path
    )
    return manager.execute_action(action)


def register_addon(addon_path: str = None, install_file: str = None) -> dict:
    """
    Register an add-on.
    Action: XSettingsRegisterAction

    Args:
        addon_path: Path where the add-on is located, e.g. "..\\addon\\1.0.0"
                    (parameter path). Alternative to install_file.
        install_file: Complete path of the install.xml file (parameter installFile).
                      Alternative to addon_path.
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    action = _build_action(
        "XSettingsRegisterAction",
        path=addon_path,
        installFile=install_file
    )
    return manager.execute_action(action)


def unregister_addon(addon_path: str = None, install_file: str = None) -> dict:
    """
    Unregister an add-on.
    Action: XSettingsUnregisterAction

    Args:
        addon_path: Path where the add-on is located (parameter path).
                    Alternative to install_file.
        install_file: Complete path of the install.xml file (parameter installFile).
                      Alternative to addon_path.
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    action = _build_action(
        "XSettingsUnregisterAction",
        path=addon_path,
        installFile=install_file
    )
    return manager.execute_action(action)


def execute_raw_action(action_string: str) -> dict:
    """
    Execute a raw EPLAN action string.
    Use this for actions not covered by specific functions.

    Args:
        action_string: Complete action string (e.g., "ActionName /PARAM1:value1 /PARAM2:value2")

    Example:
        execute_raw_action('ProjectOpen /Project:"C:\\Projects\\test.elk"')
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    return manager.execute_action(action_string)


def load_api_module_net(
    register: str = None,
    register_module: str = None,
    unregister: str = None,
    unregister_internal: str = None
) -> dict:
    """
    Load and register an API add-in written against .NET Core.
    Action: EplApiModuleActionNet

    This is the .NET Core counterpart to load_api_module (EplApiModuleAction),
    which handles the .NET Framework add-ins. Use this one for add-ins built for
    .NET Core / modern .NET; use load_api_module for legacy net48 add-ins.

    The four parameters are mutually exclusive alternatives - set exactly one
    per call.

    PERSISTENCE WARNING: registration PERSISTS for the rest of the EPLAN
    session (and, for registered modules, across restarts until unregistered).
    Any caller that registers an add-in must unregister it again in teardown,
    otherwise the next run inherits a stale add-in. Note also that EPLAN does
    not hot-reload add-ins: re-registering a rebuilt DLL silently keeps the
    already-loaded assembly, so a rebuild needs an EPLAN restart.

    Args:
        register: File name of the add-in DLL to register. A relative name is
                  resolved against the current directory.
        register_module: Register an API module - the settings must already be
                         available so the services can be created.
        unregister: Assembly file title of the add-in to unregister (the name
                    shown in the API module dialog: assembly name, no path,
                    no ".dll")
        unregister_internal: Assembly file title of the add-in to unregister; if
                             an error makes unloading impossible, the module is
                             only unregistered.
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    action = _build_action(
        "EplApiModuleActionNet",
        register=register,
        registerModule=register_module,
        unregister=unregister,
        unregisterInternal=unregister_internal
    )
    return manager.execute_action(action)


def register_custom_property_editor(
    action: str = None,
    property_id: int = None,
    property_index: int = None,
    property_ident_name: str = None,
    editable: bool = None,
    register: bool = True
) -> dict:
    """
    Register (or unregister) a custom editor dialog for a property ID or for the
    identifying name of a user-defined property.
    Action: RegisterCustomPropertyEditorAction

    PERSISTENCE WARNING: the registration PERSISTS for the rest of the EPLAN
    session - every later edit of that property routes into the registered
    action. Callers must unregister in teardown (call again with register=False
    and the same property identification), otherwise subsequent runs, and the
    interactive user, inherit the custom editor.

    Args:
        action: Name of the action that is called to edit the specified property.
                Required when registering; may be omitted when unregistering.
        property_id: Property ID to attach the editor to
        property_index: Property index
        property_ident_name: Identifying name of the user-defined property
                             (alternative to property_id / property_index)
        editable: True = editing inside the hotspot cell is allowed
        register: True = register this action, False = unregister it
    """
    manager, error = _get_connected_manager()
    if error:
        return error

    command = _build_action(
        "RegisterCustomPropertyEditorAction",
        PropertyId=property_id,
        PropertyIndex=property_index,
        PropertyIdentName=property_ident_name,
        Action=action,
        Editable=editable,
        Register=register
    )
    return manager.execute_action(command)
