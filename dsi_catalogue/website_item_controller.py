"""Keep template publication explicit and avoid duplicate Website Item creation."""
import frappe
from urllib.parse import urlparse
from webshop.webshop.doctype.website_item.website_item import WebsiteItem

class ReviewedWebsiteItem(WebsiteItem):
    def validate_website_image(self):
        # A public external image does not need a pre-existing ERP File row.
        # The publishing action creates that record in its transaction.
        image = self.website_image or ''
        parsed = urlparse(image)
        if parsed.scheme == 'https' and parsed.netloc and not parsed.username:
            return
        super().validate_website_image()
        if image and not self.website_image:
            frappe.throw('The selected image is missing or private. Choose a public image before publishing.')
    def update_template_item(self):
        # Publishing a variant must not create/publish another item implicitly.
        # Carla reviews the template separately in the Website Item queue.
        if self.variant_of and self.published:
            template = frappe.db.get_value('Website Item', {'item_code':self.variant_of}, ['name','published'], as_dict=True)
            if template and template.published:
                frappe.db.set_value('Item',self.variant_of,'published_in_website',1)
