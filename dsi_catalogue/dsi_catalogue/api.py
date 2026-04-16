import frappe
import json
import requests
import uuid
from frappe import _


# Cache keys for generation tasks
GENERATION_CACHE_PREFIX = "dsi_generation_task_"
GENERATION_CACHE_TTL = 3600  # 1 hour


def create_file_for_external_url(url, doctype=None, docname=None, filename=None):
    """
    Create a File record for an external URL (like Cloudinary).
    ERPNext can reference external URLs via the File doctype.

    Args:
        url: The external URL (e.g., Cloudinary URL)
        doctype: The doctype to attach the file to (optional)
        docname: The document name to attach the file to (optional)
        filename: Custom filename (optional, extracted from URL if not provided)

    Returns:
        The file_url that can be used in website_image field
    """
    if not url:
        return None

    # Extract filename from URL if not provided
    if not filename:
        # Handle Cloudinary URLs: extract the last part before query params
        url_path = url.split('?')[0]
        filename = url_path.split('/')[-1]
        # Ensure it has an extension
        if '.' not in filename:
            filename = filename + '.webp'

    # Check if a File record already exists for this URL
    existing = frappe.db.exists("File", {"file_url": url})
    if existing:
        return url

    # Create a new File record for the external URL
    try:
        file_doc = frappe.get_doc({
            "doctype": "File",
            "file_name": filename,
            "file_url": url,
            "is_private": 0,
            "attached_to_doctype": doctype,
            "attached_to_name": docname
        })
        file_doc.insert(ignore_permissions=True)
        frappe.db.commit()
        return url
    except Exception as e:
        frappe.log_error(f"Error creating file record for URL {url}: {str(e)}")
        return url  # Return the URL anyway, it might still work


def build_tree(catalogue_list):
    """Build hierarchical tree from flat catalogue list"""
    tree = []
    nodes = {}

    # First pass: create all nodes
    for item in catalogue_list:
        nodes[item.name] = {
            "name": item.name,
            "folder_id": item.get("folder_id"),
            "folder_path": item.get("folder_path"),
            "display_name": item.get("display_name"),
            "index_key": item.get("index_key"),
            "level_depth": item.get("level_depth", 0),
            "hero_image": item.get("hero_image"),
            "children": []
        }

    # Second pass: build relationships
    for item in catalogue_list:
        node = nodes[item.name]
        parent = item.get("parent_folder")
        if parent and parent in nodes:
            nodes[parent]["children"].append(node)
        elif not parent or item.get("level_depth", 0) <= 1:
            tree.append(node)

    return tree


@frappe.whitelist()
def get_product_catalogue_tree():
    """Return folder tree for modal"""
    catalogue = frappe.get_all("Product Catalogue",
        fields=["name", "folder_id", "folder_path", "display_name", "index_key",
                "level_depth", "parent_folder", "hero_image"],
        order_by="level_depth asc, folder_path asc"
    )
    return build_tree(catalogue)


@frappe.whitelist()
def clear_product_catalogue():
    """Clear all Product Catalogue records - used before re-sync to ensure clean data"""
    # Delete all records
    frappe.db.sql("DELETE FROM `tabProduct Catalogue`")
    frappe.db.commit()
    return {"success": True, "message": "Product Catalogue cleared successfully"}


@frappe.whitelist()
def get_folder_preview(folder_id):
    """Get preview data for folder selection"""
    doc = frappe.get_doc("Product Catalogue", folder_id)
    images = json.loads(doc.cloudinary_images or "[]")
    ai_meta = json.loads(doc.ai_metadata or "{}")

    return {
        "productName": doc.display_name,
        "indexKey": doc.index_key,
        "heroImage": doc.hero_image,
        "imageCount": len(images),
        "images": images[:6],
        "aiAnalysis": ai_meta.get("aiAnalysis", {}),
        "palace": doc.palace,
        "productRange": doc.product_range,
        "folderPath": doc.folder_path
    }


def get_or_create_item_group(palace, product_range):
    """Get or create Item Group hierarchy for Palace > Range"""
    if not palace:
        return None

    # Ensure palace exists as top-level group
    if not frappe.db.exists("Item Group", palace):
        frappe.get_doc({
            "doctype": "Item Group",
            "item_group_name": palace,
            "parent_item_group": "All Item Groups",
            "is_group": 1
        }).insert(ignore_permissions=True)

    if not product_range:
        return palace

    # Create range as child of palace
    if not frappe.db.exists("Item Group", product_range):
        frappe.get_doc({
            "doctype": "Item Group",
            "item_group_name": product_range,
            "parent_item_group": palace,
            "is_group": 0
        }).insert(ignore_permissions=True)

    return product_range


def create_or_update_slideshow(item_code, images):
    """Create or update Website Slideshow from image list"""
    if not images:
        return None

    slideshow_name = f"Slideshow-{item_code}"

    slideshow_items = []
    for img in images:
        # Truncate description to 140 chars max to avoid ERPNext field limit
        desc = img.get("alt", "") or ""
        slideshow_items.append({
            "image": img.get("url") or img.get("web_optimized"),
            "heading": (img.get("fileName", "") or "")[:140],
            "description": desc[:140] if desc else ""
        })

    if frappe.db.exists("Website Slideshow", slideshow_name):
        doc = frappe.get_doc("Website Slideshow", slideshow_name)
        doc.slideshow_items = []
        for item in slideshow_items:
            doc.append("slideshow_items", item)
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc({
            "doctype": "Website Slideshow",
            "slideshow_name": slideshow_name,
            "slideshow_items": slideshow_items
        })
        doc.insert(ignore_permissions=True)

    return slideshow_name


def create_website_route_meta(route, seo_data):
    """Create or update Website Route Meta for SEO"""
    if not route or not seo_data:
        return

    # Remove leading slash if present
    route = route.lstrip("/")

    meta_tags = []
    if seo_data.get("title"):
        meta_tags.append({"key": "title", "value": seo_data["title"]})
    if seo_data.get("description"):
        meta_tags.append({"key": "description", "value": seo_data["description"]})
    if seo_data.get("keywords"):
        keywords = seo_data["keywords"]
        if isinstance(keywords, list):
            keywords = ", ".join(keywords)
        meta_tags.append({"key": "keywords", "value": keywords})

    if not meta_tags:
        return

    if frappe.db.exists("Website Route Meta", route):
        doc = frappe.get_doc("Website Route Meta", route)
        doc.meta_tags = []
        for tag in meta_tags:
            doc.append("meta_tags", tag)
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc({
            "doctype": "Website Route Meta",
            "name": route,
            "__newname": route,
            "meta_tags": meta_tags
        })
        doc.insert(ignore_permissions=True)


@frappe.whitelist()
def publish_to_website(folder_id, item_code, generate_content=True):
    """Trigger n8n workflow and create Website Item"""
    catalogue = frappe.get_doc("Product Catalogue", folder_id)
    # n8n webhook base URL (without path)
    n8n_base_url = frappe.conf.get("n8n_webhook_url", "https://shaikh.world/webhook")

    try:
        if generate_content:
            # Trigger n8n webhook
            response = requests.post(
                f"{n8n_base_url}/erp-publish-product",
                json={
                    "folder_id": folder_id,
                    "folder_path": catalogue.folder_path,
                    "item_code": item_code,
                    "index_key": catalogue.index_key,
                    "action": "publish"
                },
                timeout=120
            )
            result = response.json()
            if result.get("status") == "processing":
                return {
                    "success": True,
                    "message": "Publishing started. Content generation in progress.",
                    "task_id": result.get("taskId")
                }
            content = result.get("content", {})
        else:
            content = {}

        # Create/Update Website Item
        return create_website_item(item_code, catalogue, content)
    except Exception as e:
        frappe.log_error(f"Error publishing to website: {str(e)}")
        return {"success": False, "error": str(e)}


def create_website_item(item_code, catalogue, content):
    """Create or update Website Item from catalogue - using standard fields"""
    images = json.loads(catalogue.cloudinary_images or "[]")

    # Get or create Item Group
    item_group = get_or_create_item_group(catalogue.palace, catalogue.product_range)

    # Create slideshow for image gallery
    slideshow_name = create_or_update_slideshow(item_code, images)

    # Build specifications as table rows
    specs = content.get("specifications", {})
    care = content.get("care_instructions", "")

    website_item_data = {
        "doctype": "Website Item",
        "item_code": item_code,
        "web_item_name": content.get("product_name") or catalogue.display_name,
        "published": 1,
        # Standard fields
        "short_description": content.get("description", ""),
        "web_long_description": content.get("product_details", ""),
        "item_group": item_group,
        "slideshow": slideshow_name,
        # Custom fields (only 2)
        "custom_repository_path": catalogue.folder_path,
        "custom_index_key": catalogue.index_key,
    }

    # Check if exists
    existing = frappe.db.exists("Website Item", {"item_code": item_code})

    if existing:
        doc = frappe.get_doc("Website Item", existing)
        doc.update(website_item_data)
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc(website_item_data)
        doc.insert(ignore_permissions=True)

    # Set primary image after doc exists (so we can attach the File to it)
    if images:
        image_url = images[0].get("url") or images[0].get("web_optimized")
        doc.website_image = create_file_for_external_url(
            image_url,
            doctype="Website Item",
            docname=doc.name,
            filename=images[0].get("fileName")
        )
        doc.save(ignore_permissions=True)

    # Add specifications as table rows
    if specs or care:
        doc.website_specifications = []
        for label, value in specs.items():
            doc.append("website_specifications", {
                "label": str(label),
                "description": str(value)
            })
        if care:
            doc.append("website_specifications", {
                "label": "Care Instructions",
                "description": care
            })
        doc.save(ignore_permissions=True)

    # Create SEO meta tags
    seo_data = {
        "title": content.get("seo_title", ""),
        "description": content.get("seo_description", ""),
        "keywords": content.get("seo_keywords", [])
    }
    if doc.route:
        create_website_route_meta(doc.route, seo_data)

    frappe.db.commit()
    return {"success": True, "website_item": doc.name}


@frappe.whitelist(allow_guest=True)
def sync_product_catalogue(folder_tree, synced_at, clear_existing=False):
    """Receive sync from n8n workflow
    
    Args:
        folder_tree: JSON tree structure of product catalogue
        synced_at: ISO datetime string for last sync time
        clear_existing: If True, completely clears the Product Catalogue table before syncing
                       This ensures a clean overwrite with no stale entries
    """
    # Convert ISO datetime to MySQL format
    from frappe.utils import get_datetime
    synced_at = get_datetime(synced_at).strftime("%Y-%m-%d %H:%M:%S") if synced_at else None

    tree = json.loads(folder_tree) if isinstance(folder_tree, str) else folder_tree
    
    # Parse clear_existing if it comes as string
    if isinstance(clear_existing, str):
        clear_existing = clear_existing.lower() in ('true', '1', 'yes')
    
    # If clear_existing is True, delete all existing records first
    if clear_existing:
        frappe.db.sql("DELETE FROM `tabProduct Catalogue`")
        frappe.db.commit()

    def process_node(node, parent=None):
        folder_id = node.get("folder_id")
        
        # When clear_existing=True, we never need to check for existing records
        name = None if clear_existing else frappe.db.exists("Product Catalogue", {"folder_id": folder_id})

        # Data for Product Catalogue - NO doctype key for set_value!
        data = {
            "folder_id": folder_id,
            "folder_path": node.get("fullPath") or node.get("folder_path"),
            "display_name": node.get("displayName") or node.get("display_name"),
            "index_key": node.get("indexKey") or node.get("index_key"),
            "level_depth": node.get("level_depth", 0),
            "parent_folder": parent,
            "palace": node.get("palace"),
            "product_range": node.get("product_range"),
            "hero_image": node.get("heroImage") or node.get("hero_image"),
            "cloudinary_images": json.dumps(node.get("cloudinary_images", [])),
            "ai_metadata": json.dumps(node.get("ai_metadata", {})),
            "last_synced": synced_at
        }

        if name:
            # Update existing - data dict must NOT contain 'doctype'
            frappe.db.set_value("Product Catalogue", name, data)
        else:
            # Insert new - add doctype for get_doc
            data["doctype"] = "Product Catalogue"
            frappe.get_doc(data).insert(ignore_permissions=True)

        for child in node.get("children", []):
            process_node(child, folder_id)

    for root in tree:
        process_node(root)

    frappe.db.commit()
    
    # Return count for verification
    count = frappe.db.count("Product Catalogue")
    return {"success": True, "message": f"Catalogue synced successfully. {count} entries.", "count": count}


@frappe.whitelist(allow_guest=True)
def receive_publish_callback(item_code, content, status="success", folder_id=None, images=None, item_group_data=None, seo_data=None):
    """Receive callback from n8n after content generation - creates or updates Website Item"""
    content = json.loads(content) if isinstance(content, str) else content
    images = json.loads(images) if isinstance(images, str) else (images or [])
    item_group_data = json.loads(item_group_data) if isinstance(item_group_data, str) else (item_group_data or {})
    seo_data = json.loads(seo_data) if isinstance(seo_data, str) else (seo_data or {})

    if status != "success":
        frappe.log_error(f"Publish callback failed for {item_code}: {content.get('error', 'Unknown error')}")
        return {"success": False}

    # Get catalogue data if folder_id provided
    catalogue = None
    if folder_id:
        try:
            catalogue = frappe.get_doc("Product Catalogue", {"folder_id": folder_id})
        except:
            pass

    # Get or create Item Group hierarchy
    item_group = None
    if item_group_data:
        item_group = get_or_create_item_group(
            item_group_data.get("palace"),
            item_group_data.get("range")
        )

    # Note: Slideshow requires local public files, skip for external URLs (Cloudinary)
    # Images will be set directly on website_image field
    slideshow_name = None

    # Check if Website Item exists
    existing = frappe.db.exists("Website Item", {"item_code": item_code})

    # Ensure we have an item_group (required by webshop)
    if not item_group:
        item_group = "All Item Groups"

    if existing:
        doc = frappe.get_doc("Website Item", existing)
    else:
        # Create new Website Item
        doc = frappe.get_doc({
            "doctype": "Website Item",
            "item_code": item_code,
            "web_item_name": content.get("product_name") or (catalogue.display_name if catalogue else item_code),
            "published": 1,
            "item_group": item_group
        })
        doc.insert(ignore_permissions=True)

    # Update standard fields
    doc.short_description = content.get("description", "")
    doc.web_long_description = content.get("product_details", "")

    if item_group:
        doc.item_group = item_group
    if slideshow_name:
        doc.slideshow = slideshow_name
    if images:
        image_url = images[0].get("cloudinaryUrl") or images[0].get("url") or images[0].get("web_optimized")
        # Create a File record for the external URL so ERPNext can reference it
        doc.website_image = create_file_for_external_url(
            image_url,
            doctype="Website Item",
            docname=doc.name,
            filename=images[0].get("fileName")
        )

    # Custom fields
    if catalogue:
        doc.custom_repository_path = catalogue.folder_path
        doc.custom_index_key = catalogue.index_key

    # SEO custom fields - store directly on Website Item
    if seo_data:
        doc.custom_seo_title = seo_data.get("title", "")
        doc.custom_seo_description = seo_data.get("description", "")
        # Convert keywords array to comma-separated string
        keywords = seo_data.get("keywords", [])
        if isinstance(keywords, list):
            doc.custom_seo_keywords = ", ".join(keywords)
        else:
            doc.custom_seo_keywords = keywords or ""

    # Additional content fields
    doc.custom_marketing_headline = content.get("marketing_headline", "")
    doc.custom_luxury_score = content.get("luxury_score", 0)

    # General product line description (shared across variants)
    if content.get("general_description"):
        doc.website_content = content.get("general_description")

    # Image alt text from first image
    if images:
        first_image_name = images[0].get("fileName", "")
        image_alts = content.get("image_alts", {})
        alt_text = image_alts.get(first_image_name, "") or images[0].get("description", "")
        # Truncate to 140 chars max to avoid ERPNext field limit error
        doc.custom_image_alt_text = alt_text[:140] if alt_text else ""

    # Update specifications table
    specs = content.get("specifications", {})
    care = content.get("care_instructions", "")
    if specs or care:
        doc.website_specifications = []
        for label, value in specs.items():
            doc.append("website_specifications", {
                "label": str(label),
                "description": str(value)
            })
        if care:
            doc.append("website_specifications", {
                "label": "Care Instructions",
                "description": care
            })

    doc.save(ignore_permissions=True)

    # Reload to get auto-generated route
    doc.reload()

    # Update SEO via Website Route Meta
    if not seo_data:
        seo_data = {
            "title": content.get("seo_title", ""),
            "description": content.get("seo_description", ""),
            "keywords": content.get("seo_keywords", [])
        }

    # If route still not available, generate one based on web_item_name
    route = doc.route
    if not route:
        from frappe.website.utils import cleanup_page_name
        route = cleanup_page_name(doc.web_item_name)
        doc.route = route
        doc.save(ignore_permissions=True)

    if route and seo_data:
        create_website_route_meta(route, seo_data)

    frappe.db.commit()
    return {"success": True, "website_item": doc.name, "route": doc.route, "created": not existing}


@frappe.whitelist()
def get_publish_status(task_id):
    """Check the status of a publish task"""
    return {
        "task_id": task_id,
        "status": "processing",
        "message": "Content generation in progress..."
    }


@frappe.whitelist()
def start_content_generation(folder_id, item_code, temperature=0.7):
    """Start async content generation, return task_id for polling"""
    # Generate unique task ID
    task_id = str(uuid.uuid4())

    # Get catalogue data
    catalogue = frappe.get_doc("Product Catalogue", folder_id)

    # Store task in cache with initial status
    cache_key = f"{GENERATION_CACHE_PREFIX}{task_id}"
    frappe.cache.set_value(cache_key, {
        "status": "processing",
        "folder_id": folder_id,
        "item_code": item_code,
        "temperature": float(temperature),
        "content": None,
        "images": None,
        "seo_data": None,
        "item_group_data": None,
        "error": None
    }, expires_in_sec=GENERATION_CACHE_TTL)

    # Get n8n webhook URL
    n8n_base_url = frappe.conf.get("n8n_webhook_url", "https://shaikh.world/webhook")

    try:
        # Trigger n8n webhook with task_id and temperature
        response = requests.post(
            f"{n8n_base_url}/erp-publish-product",
            json={
                "task_id": task_id,
                "folder_id": folder_id,
                "folder_path": catalogue.folder_path,
                "item_code": item_code,
                "index_key": catalogue.index_key,
                "temperature": float(temperature),
                "action": "generate"
            },
            timeout=30  # Short timeout - n8n will callback
        )

        return {
            "success": True,
            "task_id": task_id,
            "message": "Content generation started"
        }
    except Exception as e:
        # Update cache with error
        frappe.cache.set_value(cache_key, {
            "status": "error",
            "error": str(e)
        }, expires_in_sec=GENERATION_CACHE_TTL)

        frappe.log_error(f"Error starting content generation: {str(e)}")
        return {"success": False, "error": str(e)}


@frappe.whitelist()
def get_generation_status(task_id):
    """Poll for generation status and content"""
    cache_key = f"{GENERATION_CACHE_PREFIX}{task_id}"
    task_data = frappe.cache.get_value(cache_key)

    if not task_data:
        return {
            "status": "not_found",
            "error": "Task not found or expired"
        }

    return task_data


@frappe.whitelist(allow_guest=True)
def receive_generation_callback(task_id, content, images=None, seo_data=None, item_group_data=None, status="success", error=None):
    """Receive generated content from n8n - store for preview, don't create item"""
    # Parse JSON strings
    content = json.loads(content) if isinstance(content, str) else content
    images = json.loads(images) if isinstance(images, str) else (images or [])
    seo_data = json.loads(seo_data) if isinstance(seo_data, str) else (seo_data or {})
    item_group_data = json.loads(item_group_data) if isinstance(item_group_data, str) else (item_group_data or {})

    cache_key = f"{GENERATION_CACHE_PREFIX}{task_id}"

    if status != "success":
        # Store error in cache
        frappe.cache.set_value(cache_key, {
            "status": "error",
            "error": error or content.get("error", "Unknown error")
        }, expires_in_sec=GENERATION_CACHE_TTL)
        return {"success": False}

    # Store generated content in cache for preview
    frappe.cache.set_value(cache_key, {
        "status": "completed",
        "content": content,
        "images": images,
        "seo_data": seo_data,
        "item_group_data": item_group_data,
        "error": None
    }, expires_in_sec=GENERATION_CACHE_TTL)

    return {"success": True, "message": "Content stored for preview"}


@frappe.whitelist(allow_guest=True)
def get_published_website_items():
    """Return all published Website Items with custom fields for the shop page.

    This endpoint bypasses guest API restrictions that prevent querying custom fields.
    Returns all fields including custom_index_key for product routing.
    """
    items = frappe.get_all(
        "Website Item",
        filters={"published": 1},
        fields=[
            "name",
            "item_code",
            "web_item_name",
            "website_image",
            "short_description",
            "web_long_description",
            "item_group",
            "route",
            "slideshow",
            "custom_index_key",
            "custom_repository_path"
        ]
    )
    return items


@frappe.whitelist(allow_guest=True)
def get_website_item_by_index_key(index_key):
    """Get a single Website Item by its custom_index_key.

    Args:
        index_key: The index key like {F-CR-AK-AC}

    Returns:
        Website Item data or None
    """
    if not index_key:
        return None

    items = frappe.get_all(
        "Website Item",
        filters={
            "published": 1,
            "custom_index_key": index_key
        },
        fields=[
            "name",
            "item_code",
            "web_item_name",
            "website_image",
            "short_description",
            "web_long_description",
            "item_group",
            "route",
            "slideshow",
            "custom_index_key",
            "custom_repository_path"
        ],
        limit=1
    )

    if not items:
        return None

    item = items[0]

    # Get specifications table
    specs = frappe.get_all(
        "Website Item Website Specification",
        filters={"parent": item.name},
        fields=["label", "description"]
    )
    item["specifications"] = specs

    # Get slideshow images if exists
    if item.get("slideshow"):
        slideshow_items = frappe.get_all(
            "Website Slideshow Item",
            filters={"parent": item.slideshow},
            fields=["image", "heading", "description"],
            order_by="idx"
        )
        item["slideshow_images"] = slideshow_items

    return item


@frappe.whitelist(allow_guest=True)
def get_general_description_for_product(template_key):
    """Find any sibling variant's general description for this product line.

    Searches Website Items where custom_index_key starts with template_key
    and has a non-empty website_content (Advanced Display Content).

    Args:
        template_key: The template index key like {F-CR-AK}

    Returns:
        dict with general_description or None
    """
    if not template_key:
        return {"general_description": None}

    # Build LIKE pattern: {F-CR-AK% matches {F-CR-AK}, {F-CR-AK-M}, {F-CR-AK-AC}, etc.
    pattern = template_key.rstrip('}') + '%}'

    result = frappe.db.sql("""
        SELECT website_content
        FROM `tabWebsite Item`
        WHERE custom_index_key LIKE %s
        AND website_content IS NOT NULL
        AND website_content != ''
        ORDER BY creation ASC
        LIMIT 1
    """, pattern, as_dict=True)

    if result:
        return {"general_description": result[0].get("website_content")}
    return {"general_description": None}


@frappe.whitelist()
def publish_website_item(folder_id, item_code, content, images=None, seo_data=None, item_group_data=None):
    """Create/update Website Item from previewed content"""
    # Parse JSON strings
    content = json.loads(content) if isinstance(content, str) else content
    images = json.loads(images) if isinstance(images, str) else (images or [])
    seo_data = json.loads(seo_data) if isinstance(seo_data, str) else (seo_data or {})
    item_group_data = json.loads(item_group_data) if isinstance(item_group_data, str) else (item_group_data or {})

    try:
        # Get catalogue data
        catalogue = None
        if folder_id:
            try:
                catalogue = frappe.get_doc("Product Catalogue", folder_id)
            except:
                pass

        # Get or create Item Group hierarchy
        item_group = None
        if item_group_data:
            item_group = get_or_create_item_group(
                item_group_data.get("palace"),
                item_group_data.get("range")
            )

        # Ensure we have an item_group (required by webshop)
        if not item_group:
            item_group = "All Item Groups"

        # Check if Website Item exists
        existing = frappe.db.exists("Website Item", {"item_code": item_code})

        if existing:
            doc = frappe.get_doc("Website Item", existing)
        else:
            # Create new Website Item
            doc = frappe.get_doc({
                "doctype": "Website Item",
                "item_code": item_code,
                "web_item_name": content.get("product_name") or (catalogue.display_name if catalogue else item_code),
                "published": 1,
                "item_group": item_group
            })
            doc.insert(ignore_permissions=True)

        # Update standard fields
        doc.web_item_name = content.get("product_name") or doc.web_item_name
        doc.short_description = content.get("description", "")
        doc.web_long_description = content.get("product_details", "")
        doc.item_group = item_group

        # Set primary image with alt text
        if images:
            primary_img = images[0]
            image_url = primary_img.get("cloudinaryUrl") or primary_img.get("url") or primary_img.get("web_optimized")
            # Create a File record for the external URL so ERPNext can reference it
            doc.website_image = create_file_for_external_url(
                image_url,
                doctype="Website Item",
                docname=doc.name,
                filename=primary_img.get("fileName")
            )
            # Store alt text in image alt field if available
            if hasattr(doc, "website_image_alt"):
                doc.website_image_alt = primary_img.get("alt") or content.get("image_alts", {}).get(primary_img.get("fileName"), "")

        # Custom fields
        if catalogue:
            doc.custom_repository_path = catalogue.folder_path
            doc.custom_index_key = catalogue.index_key

        # SEO custom fields - store directly on Website Item
        if seo_data:
            doc.custom_seo_title = seo_data.get("title", "")
            doc.custom_seo_description = seo_data.get("description", "")
            # Convert keywords array to comma-separated string
            keywords = seo_data.get("keywords", [])
            if isinstance(keywords, list):
                doc.custom_seo_keywords = ", ".join(keywords)
            else:
                doc.custom_seo_keywords = keywords or ""

        # Additional content fields
        doc.custom_marketing_headline = content.get("marketing_headline", "")
        doc.custom_luxury_score = content.get("luxury_score", 0)

        # General product line description (shared across variants)
        if content.get("general_description"):
            doc.website_content = content.get("general_description")

        # Image alt text from first image
        if images:
            primary_img = images[0]
            first_image_name = primary_img.get("fileName", "")
            image_alts = content.get("image_alts", {})
            doc.custom_image_alt_text = image_alts.get(first_image_name, "") or primary_img.get("description", "")

        # Update specifications table
        specs = content.get("specifications", {})
        care = content.get("care_instructions", "")
        if specs or care:
            doc.website_specifications = []
            for label, value in specs.items():
                doc.append("website_specifications", {
                    "label": str(label),
                    "description": str(value)
                })
            if care:
                doc.append("website_specifications", {
                    "label": "Care Instructions",
                    "description": care
                })

        # Store image alts in specifications if we have them
        image_alts = content.get("image_alts", {})
        if image_alts:
            doc.append("website_specifications", {
                "label": "Image Descriptions",
                "description": json.dumps(image_alts)
            })

        doc.save(ignore_permissions=True)

        # Reload to get auto-generated route
        doc.reload()

        # Create SEO meta tags
        if seo_data:
            # If route still not available, generate one based on web_item_name
            route = doc.route
            if not route:
                # Generate route from web_item_name (same logic ERPNext uses)
                from frappe.website.utils import cleanup_page_name
                route = cleanup_page_name(doc.web_item_name)
                doc.route = route
                doc.save(ignore_permissions=True)

            if route:
                create_website_route_meta(route, seo_data)

        frappe.db.commit()
        return {"success": True, "website_item": doc.name, "route": doc.route, "created": not existing}

    except Exception as e:
        frappe.log_error(f"Error publishing website item: {str(e)}")
        return {"success": False, "error": str(e)}


@frappe.whitelist(allow_guest=True)
def get_product_images_by_index_key(index_key_prefix=None):
    """Get product images for items matching an index key prefix.

    Searches Website Items, Slideshows, and File attachments.

    Args:
        index_key_prefix: Partial index key like "{P-DQ-FP}" to match items starting with it.

    Returns:
        List of image dicts: [{id, url, alt, fileName, sharedType, variantCode, isHero, qualityScore}]
    """
    if not index_key_prefix:
        return []

    images = []
    seen_urls = set()

    # 1. Find matching Website Items by index key prefix
    website_items = frappe.get_all(
        "Website Item",
        filters=[
            ["published", "=", 1],
            ["custom_index_key", "like", f"{index_key_prefix}%"]
        ],
        fields=["name", "item_code", "web_item_name", "website_image",
                "slideshow", "custom_index_key"],
        order_by="custom_index_key asc"
    )

    for idx, wi in enumerate(website_items):
        index_key = wi.get("custom_index_key") or ""

        # Extract variant code from index key: {F-CR-AK-AC} -> last segment = AC
        variant_code = ""
        if index_key:
            parts = index_key.strip("{}").split("-")
            # Variant code is everything after the prefix parts
            prefix_parts = index_key_prefix.strip("{}").split("-")
            if len(parts) > len(prefix_parts):
                variant_code = "-".join(parts[len(prefix_parts):])

        # Add website_image if available
        if wi.website_image and wi.website_image not in seen_urls:
            seen_urls.add(wi.website_image)
            file_name = wi.website_image.split("/")[-1] if "/" in wi.website_image else wi.website_image
            images.append({
                "id": f"wi-{wi.name}-hero",
                "url": wi.website_image,
                "alt": wi.web_item_name or file_name,
                "fileName": file_name,
                "sharedType": "variant" if variant_code else "inclusive",
                "variantCode": variant_code,
                "isHero": idx == 0,
                "qualityScore": 100 - idx,
            })

        # 2. Get slideshow images if available
        if wi.slideshow:
            slideshow_images = frappe.get_all(
                "Website Slideshow Item",
                filters={"parent": wi.slideshow},
                fields=["image", "heading", "description"],
                order_by="idx asc"
            )
            for si_idx, si in enumerate(slideshow_images):
                if si.image and si.image not in seen_urls:
                    seen_urls.add(si.image)
                    file_name = si.image.split("/")[-1] if "/" in si.image else si.image
                    images.append({
                        "id": f"ss-{wi.name}-{si_idx}",
                        "url": si.image,
                        "alt": si.heading or si.description or file_name,
                        "fileName": file_name,
                        "sharedType": "variant" if variant_code else "inclusive",
                        "variantCode": variant_code,
                        "isHero": False,
                        "qualityScore": 80 - si_idx,
                    })

        # 3. Get attached File images
        file_images = frappe.get_all(
            "File",
            filters={
                "attached_to_doctype": "Website Item",
                "attached_to_name": wi.name,
                "is_private": 0,
            },
            fields=["file_url", "file_name"],
            order_by="creation asc"
        )
        for fi_idx, fi in enumerate(file_images):
            url = fi.file_url or ""
            if url and url.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")) and url not in seen_urls:
                seen_urls.add(url)
                # Make absolute URL if relative
                if not url.startswith("http"):
                    url = frappe.utils.get_url() + url
                images.append({
                    "id": f"file-{wi.name}-{fi_idx}",
                    "url": url,
                    "alt": fi.file_name or "",
                    "fileName": fi.file_name or "",
                    "sharedType": "variant" if variant_code else "inclusive",
                    "variantCode": variant_code,
                    "isHero": False,
                    "qualityScore": 60 - fi_idx,
                })

    # 4. Also check Product Catalogue if it has data
    catalogue_items = frappe.get_all(
        "Product Catalogue",
        filters=[["index_key", "like", f"{index_key_prefix}%"]],
        fields=["name", "index_key", "cloudinary_images", "hero_image"],
    )
    for cat in catalogue_items:
        if cat.hero_image and cat.hero_image not in seen_urls:
            seen_urls.add(cat.hero_image)
            images.append({
                "id": f"cat-hero-{cat.name}",
                "url": cat.hero_image,
                "alt": cat.name,
                "fileName": cat.hero_image.split("/")[-1] if "/" in cat.hero_image else "",
                "sharedType": "inclusive",
                "variantCode": "",
                "isHero": True,
                "qualityScore": 95,
            })
        if cat.cloudinary_images:
            try:
                cloud_imgs = json.loads(cat.cloudinary_images) if isinstance(cat.cloudinary_images, str) else cat.cloudinary_images
                if isinstance(cloud_imgs, list):
                    for ci_idx, ci in enumerate(cloud_imgs):
                        url = ci.get("url") or ci.get("secure_url") or ""
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            images.append({
                                "id": ci.get("public_id") or f"cloud-{cat.name}-{ci_idx}",
                                "url": url,
                                "alt": ci.get("alt") or ci.get("public_id") or "",
                                "fileName": url.split("/")[-1] if "/" in url else "",
                                "sharedType": ci.get("sharedType") or "inclusive",
                                "variantCode": ci.get("variantCode") or "",
                                "isHero": ci.get("isHero") or False,
                                "qualityScore": ci.get("qualityScore") or (90 - ci_idx),
                            })
            except (json.JSONDecodeError, TypeError):
                pass

    return images
