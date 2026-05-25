"""Subprocess-internal modules for hermes-plugin-http-backplane.

The package's public API (``register_integration``) lives at the parent
package root (``hermes_plugin_http_backplane.api``). This subpackage holds the
HTTP server entry point and its dependencies — everything that runs in
the spawned ``python -m runtime.server`` subprocess.
"""
