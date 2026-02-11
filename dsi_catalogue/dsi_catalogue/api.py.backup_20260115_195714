import frappe
import json
import requests
import uuid
from frappe import _


# Cache keys for generation tasks
GENERATION_CACHE_PREFIX = "dsi_generation_task_"
GENERATION_CACHE_TTL = 3600  # 1 hour


def build_tree(catalogue_list):
    """Build hierarchical tree from flat catalogue list"""
    tree = []
    nodes = {}

    # First pass: create all nodes with ALL fields
    for item in catalogue_list:
        # Convert last_synced to ISO format with UTC timezone for proper JS handling
        last_synced_val = item.get("last_synced")
        if last_synced_val:
            # Add 'Z' suffix to indicate UTC timezone
            last_synced_str = str(last_synced_val).replace(" ", "T") + "Z"
        else:
            last_synced_str = None
            
        nodes[item.name] = {
            "name": item.name,
            "folder_id": item.get("folder_id"),
            "folder_path": item.get("folder_path"),
            "display_name": item.get("display_name"),
            "index_key": item.get("index_key"),
            "level_depth": item.get("level_depth", 0),
            "hero_image": item.get("hero_image"),
            "palace": item.get("palace"),
            "product_range": item.get("product_range"),
            "last_synced": last_synced_str,
            "cloudinary_images": item.get("cloudinary_images"),
            "ai_metadata": item.get("ai_metadata"),
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
    """Return folder tree for modal - always fetches fresh data"""
    # Ensure we get fresh data from database (bypass any ORM cache)
    frappe.db.commit()
    
    catalogue = frappe.get_all("Product Catalogue",
        fields=["name", "folder_id", "folder_path", "display_name", "index_key",
                "level_depth", "parent_folder", "hero_image", "palace", "product_range",
                "last_synced", "cloudinary_images", "ai_metadata"],
        order_by="level_depth asc, folder_path asc"
    )
    return build_tree(catalogue)


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
        # Get image URL - support both cloudinaryUrl and url keys
        img_url = img.get("cloudinaryUrl") or img.get("url") or img.get("web_optimized")
        if img_url:
            # Truncate heading and description to avoid field length errors
            heading = (img.get("fileName", "") or "")[:140]
            description = (img.get("alt", "") or "")[:140]
            slideshow_items.append({
                "image": img_url,
                "heading": heading,
                "description": description
            })

    if not slideshow_items:
        return None

    if frappe.db.exists("Website Slideshow", slideshow_name):
        doc = frappe.get_doc("Website Slideshow", slideshow_name)
        doc.slideshow_items = []
        for item in slideshow_items:
            doc.append("slideshow_items", item)
        # Skip validation for external URLs (Cloudinary)
        doc.flags.ignore_validate = True
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc({
            "doctype": "Website Slideshow",
            "slideshow_name": slideshow_name,
            "slideshow_items": slideshow_items
        })
        # Skip validation for external URLs (Cloudinary)
        doc.flags.ignore_validate = True
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

    # Set primary image
    if images:
        website_item_data["website_image"] = images[0].get("url") or images[0].get("web_optimized")

    # Check if exists
    existing = frappe.db.exists("Website Item", {"item_code": item_code})

    if existing:
        doc = frappe.get_doc("Website Item", existing)
        doc.update(website_item_data)
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc(website_item_data)
        doc.insert(ignore_permissions=True)

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
def sync_product_catalogue(folder_tree, synced_at):
    """Receive sync from n8n workflow"""
    # Convert ISO datetime to MySQL format
    from frappe.utils import get_datetime
    synced_at = get_datetime(synced_at).strftime("%Y-%m-%d %H:%M:%S") if synced_at else None

    tree = json.loads(folder_tree) if isinstance(folder_tree, str) else folder_tree

    def process_node(node, parent=None):
        folder_id = node.get("folder_id")
        name = frappe.db.exists("Product Catalogue", {"folder_id": folder_id})

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
    return {"success": True, "message": "Catalogue synced successfully"}


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
        doc.website_image = images[0].get("cloudinaryUrl") or images[0].get("url") or images[0].get("web_optimized")

    # Custom fields
    if catalogue:
        doc.custom_repository_path = catalogue.folder_path
        doc.custom_index_key = catalogue.index_key

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

    # Update SEO via Website Route Meta
    if not seo_data:
        seo_data = {
            "title": content.get("seo_title", ""),
            "description": content.get("seo_description", ""),
            "keywords": content.get("seo_keywords", [])
        }
    if doc.route:
        create_website_route_meta(doc.route, seo_data)

    frappe.db.commit()
    return {"success": True, "website_item": doc.name, "created": not existing}


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
            img_url = primary_img.get("cloudinaryUrl") or primary_img.get("url") or primary_img.get("web_optimized")
            doc.website_image = img_url
            # Store alt text in image alt field if available (truncate to 140 chars)
            if hasattr(doc, "website_image_alt"):
                alt_text = primary_img.get("alt") or content.get("image_alts", {}).get(primary_img.get("fileName"), "")
                doc.website_image_alt = alt_text[:140] if alt_text else ""

            # Create slideshow for image gallery
            slideshow_name = create_or_update_slideshow(item_code, images)
            if slideshow_name:
                doc.slideshow = slideshow_name

        # Custom fields
        if catalogue:
            doc.custom_repository_path = catalogue.folder_path
            doc.custom_index_key = catalogue.index_key

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

        # Skip validation for external image URLs (Cloudinary)
        doc.flags.ignore_validate = True
        doc.save(ignore_permissions=True)

        # Create SEO meta tags
        if seo_data and doc.route:
            create_website_route_meta(doc.route, seo_data)

        frappe.db.commit()
        return {"success": True, "website_item": doc.name, "created": not existing}

    except Exception as e:
        frappe.log_error(f"Error publishing website item: {str(e)}")
        return {"success": False, "error": str(e)}
