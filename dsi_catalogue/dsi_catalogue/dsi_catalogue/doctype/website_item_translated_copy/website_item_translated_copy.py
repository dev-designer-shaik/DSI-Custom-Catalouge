# Copyright (c) 2026, Designer Shaik and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WebsiteItemTranslatedCopy(Document):
    """Child row: one authored translation of a Website Item, per language.

    English is NOT stored here - it lives in the canonical Website Item fields.
    Rows are written by dsi_catalogue.i18n.put_copy, never by a grid.
    """

    pass
