import json
from pathlib import Path

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

MODULE = "Frappe Slack Connector"


def install() -> None:
    """Apply all Slack Connector customisations (idempotent; runs on install and migrate)."""
    install_custom_fields()
    install_property_setters()


def install_custom_fields() -> None:
    """Install / refresh custom fields from custom_fields.json."""
    data_file = Path(__file__).parent / "custom_fields.json"
    custom_fields: dict[str, list[dict]] = json.loads(data_file.read_text())
    # Tag every field with the app's module so /desk/custom-field shows which app owns it.
    for fields in custom_fields.values():
        for field in fields:
            field["module"] = MODULE
    create_custom_fields(custom_fields, update=True)


def install_property_setters() -> None:
    """Install / refresh property setters from property_setters.json, tagged with the app's module.

    make_property_setter() can't set a module, so each one is written here with it: the module is how this
    app's property setters are told apart from other apps' on the same DocType.
    """
    data_file = Path(__file__).parent / "property_setters.json"
    property_setters: list[dict] = json.loads(data_file.read_text())
    # Tag every property setter with the app's module, as the custom fields are.
    for ps in property_setters:
        ps["module"] = MODULE

    for ps in property_setters:
        doc = frappe.get_doc(
            {
                "doctype": "Property Setter",
                "doctype_or_field": "DocType" if ps.get("for_doctype") else "DocField",
                "doc_type": ps["doctype"],
                "field_name": ps.get("fieldname"),
                "property": ps["property"],
                "value": ps["value"],
                "property_type": ps["property_type"],
                "module": ps["module"],
            }
        )
        # Runs from after_install / after_migrate, not from a user request: these are the app's own
        # customisations, so they are applied regardless of who runs the install or migrate.
        doc.flags.ignore_permissions = True
        # A new Property Setter's validate() deletes the existing one with the same name, so this replaces it
        # with the value and module above.
        doc.insert(ignore_if_duplicate=True)
