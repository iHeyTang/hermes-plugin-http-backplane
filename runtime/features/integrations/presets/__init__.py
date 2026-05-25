"""Built-in integrations shipped with the backplane.

Each sub-package is a self-contained integration: an ``integration.yaml``
for metadata plus a ``setup(router)`` callable exposed from the package
root. The loader discovers them via :func:`pkgutil.iter_modules`.

Adding a preset is therefore just "drop a directory in here". To turn a
preset into a user-editable integration, copy it under
``~/.hermes/integrations/<name>/``; the loader will load the user copy
instead (presets and user dirs sharing a name is rejected with a clear
log line, presets winning today).
"""
