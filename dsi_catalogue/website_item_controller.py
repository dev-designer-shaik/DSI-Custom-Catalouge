"""Keep template publication explicit and avoid duplicate Website Item creation."""
import frappe
from webshop.webshop.doctype.website_item.website_item import WebsiteItem

class ReviewedWebsiteItem(WebsiteItem):
    def update_template_item(self):
        # Publishing a variant must not create/publish another item implicitly.
        # Carla reviews the template separately in the Website Item queue.
        if self.variant_of and self.published:
            template = frappe.db.get_value('Website Item', {'item_code':self.variant_of}, ['name','published'], as_dict=True)
            if template and template.published:
                frappe.db.set_value('Item',self.variant_of,'published_in_website',1)
