"""
Delete all redundant custom fields from Website Item.
Keep only: custom_repository_path, custom_index_key

Run with:
bench --site erp1.shaik.net execute dsi_catalogue.delete_redundant_fields.execute
"""

import frappe

# ALL custom fields to DELETE (everything except repository_path and index_key)
FIELDS_TO_DELETE = [
    # Section breaks
    "custom_repository_section",
    "custom_variant_section",
    "custom_content_section",
    "custom_column_break_repo",
    "custom_media_section",
    "custom_ai_section",

    # Redundant - use item_group hierarchy
    "custom_palace",
    "custom_range",

    # Redundant - use standard variant_of
    "custom_variant_of",
    "custom_variant_name",
    "custom_variant_code",
    "custom_related_variants",

    # Redundant - use standard short_description
    "custom_short_description",

    # Redundant - use standard web_long_description
    "custom_long_description",

    # Redundant - use website_specifications table
    "custom_specifications",
    "custom_care_instructions",

    # Redundant - use Website Slideshow doctype
    "custom_image_gallery",

    # Redundant - use Website Route Meta
    "custom_seo_data",

    # No longer needed
    "custom_delivery_info",
    "custom_ai_metadata",
]


def execute():
    """Delete redundant custom fields"""
    print("Deleting redundant Website Item custom fields...")
    print("Keeping only: custom_repository_path, custom_index_key")
    print("-" * 50)

    deleted = 0
    not_found = 0

    for fieldname in FIELDS_TO_DELETE:
        doc_name = f"Website Item-{fieldname}"
        if frappe.db.exists("Custom Field", doc_name):
            frappe.delete_doc("Custom Field", doc_name, force=True)
            print(f"  Deleted: {fieldname}")
            deleted += 1
        else:
            print(f"  Not found: {fieldname}")
            not_found += 1

    frappe.db.commit()

    print("-" * 50)
    print(f"Deleted: {deleted}, Not found: {not_found}")
    print("Done!")


if __name__ == "__main__":
    execute()
