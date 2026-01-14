# DSI Catalogue - ERPNext Custom App

A custom Frappe/ERPNext application for managing product catalogue sync and website publishing with n8n workflow integration.

## Overview

This app provides:
- A custom `Product Catalogue` DocType for staging product data from external sources
- Integration with n8n workflows for content generation and catalogue sync
- A custom "Publish to Website" modal on the Item form
- Cloudinary image support with AI-generated content

---

## Installation Location

**Server:** `ubuntu@ec2-16-24-74-200.me-south-1.compute.amazonaws.com`
**Path:** `/home/ubuntu/frappe-bench/apps/dsi_catalogue/`
**Site:** `erp1.shaik.net`

---

## Custom DocTypes

### Product Catalogue

**Purpose:** Staging table for product folder data synced from external repository via n8n

| Field | Type | Description |
|-------|------|-------------|
| `folder_id` | Data (Primary Key) | Unique Google Drive folder ID |
| `folder_path` | Data | Full path in repository (e.g., `DSI-Product-Repository/Palace-of-Fragrances/...`) |
| `display_name` | Data | Human-readable product/folder name |
| `index_key` | Data | Encoded product identifier (e.g., `{F-CR-OSA-W-40}`) |
| `level_depth` | Int | Depth in folder hierarchy |
| `parent_folder` | Link (self) | Reference to parent Product Catalogue entry |
| `palace` | Data | Top-level category (Palace-of-Fragrances, Palace-of-Style, etc.) |
| `product_range` | Data | Product range (CLASSIC RANGE, PRECIOUS RANGE, etc.) |
| `hero_image` | Data | Cloudinary URL for primary image |
| `cloudinary_images` | JSON | Array of all Cloudinary image objects with metadata |
| `ai_metadata` | JSON | AI analysis data (quality scores, descriptions, etc.) |
| `last_synced` | Datetime | Timestamp of last sync from n8n |

**File:** `dsi_catalogue/dsi_catalogue/doctype/product_catalogue/product_catalogue.json`

---

## Whitelisted API Functions

All functions are in `dsi_catalogue/api.py`:

### Public APIs (allow_guest=True)

| Function | Endpoint | Description |
|----------|----------|-------------|
| `sync_product_catalogue` | `/api/method/dsi_catalogue.api.sync_product_catalogue` | Receives folder tree from n8n and syncs to Product Catalogue DocType |
| `receive_publish_callback` | `/api/method/dsi_catalogue.api.receive_publish_callback` | Legacy callback - receives generated content from n8n and creates Website Item |
| `receive_generation_callback` | `/api/method/dsi_catalogue.api.receive_generation_callback` | Receives AI-generated content from n8n, stores in cache for preview |

### Authenticated APIs

| Function | Endpoint | Description |
|----------|----------|-------------|
| `get_product_catalogue_tree` | `/api/method/dsi_catalogue.api.get_product_catalogue_tree` | Returns hierarchical tree of Product Catalogue for modal display |
| `get_folder_preview` | `/api/method/dsi_catalogue.api.get_folder_preview` | Returns preview data for selected folder (images, AI analysis) |
| `publish_to_website` | `/api/method/dsi_catalogue.api.publish_to_website` | Triggers n8n workflow to generate content and create Website Item |
| `start_content_generation` | `/api/method/dsi_catalogue.api.start_content_generation` | Starts async content generation, returns task_id for polling |
| `get_generation_status` | `/api/method/dsi_catalogue.api.get_generation_status` | Polls for generation status (processing/completed/error) |
| `publish_website_item` | `/api/method/dsi_catalogue.api.publish_website_item` | Creates/updates Website Item from previewed content |
| `get_publish_status` | `/api/method/dsi_catalogue.api.get_publish_status` | Checks status of publish task |

---

## Helper Functions (Internal)

| Function | Description |
|----------|-------------|
| `build_tree()` | Converts flat Product Catalogue list to hierarchical tree |
| `get_or_create_item_group()` | Creates Item Group hierarchy (Palace > Range) |
| `create_or_update_slideshow()` | Creates Website Slideshow from Cloudinary images |
| `create_website_route_meta()` | Creates/updates SEO meta tags for Website Item routes |
| `create_website_item()` | Core function to create/update Website Item with all fields |

---

## Custom Fields on Standard DocTypes

### Website Item

| Field | Type | Description |
|-------|------|-------------|
| `custom_repository_path` | Data | Full repository path from Product Catalogue |
| `custom_index_key` | Data | Product index key from Product Catalogue |

---

## Client-Side Scripts

### item_publish.js

**Location:** `dsi_catalogue/public/js/item_publish.js`

Adds a custom "Publish to Website" button to the Item form that opens a modal with:

1. **Folder Tree Panel** - Browse Product Catalogue hierarchy
2. **Preview Panel** - Shows selected folder's images and metadata
3. **Generate Content** - Triggers AI content generation via n8n
4. **Content Preview Modal** - Shows generated content before publishing
5. **Regenerate** - Allows adjusting AI temperature and regenerating
6. **Publish** - Creates/updates Website Item with content

**Classes:**
- `PublishToWebsiteModal` - Main folder selection modal
- `ContentPreviewModal` - Generated content preview and publish modal

---

## Hooks Configuration

**File:** `dsi_catalogue/hooks.py`

```python
app_name = "dsi_catalogue"
app_title = "DSI Product Catalogue"

# Include JS/CSS globally
app_include_js = "/assets/dsi_catalogue/js/item_publish.js"
app_include_css = "/assets/dsi_catalogue/css/publish_modal.css"

# DocType JS - Form scripts
doctype_js = {
    "Item": "public/js/item_publish.js"
}
```

---

## n8n Integration

### Webhooks Used

| Webhook | Direction | Purpose |
|---------|-----------|---------|
| `POST /webhook/erp-publish-product` | ERPNext → n8n | Triggers content generation |
| `POST /api/method/dsi_catalogue.api.sync_product_catalogue` | n8n → ERPNext | Syncs folder tree |
| `POST /api/method/dsi_catalogue.api.receive_generation_callback` | n8n → ERPNext | Returns generated content |

### Configuration

n8n webhook URL is configured in `site_config.json`:
```json
{
  "n8n_webhook_url": "https://shaikh.world/webhook"
}
```

---

## Data Flow

### Catalogue Sync Flow
```
n8n Folder Sync Workflow
    ↓
POST /api/method/dsi_catalogue.api.sync_product_catalogue
    ↓
Product Catalogue DocType (staging table)
```

### Content Generation Flow
```
User clicks "Publish to Website" on Item form
    ↓
Modal shows Product Catalogue tree
    ↓
User selects folder → Preview loads
    ↓
User clicks "Generate Content"
    ↓
POST to n8n webhook (async)
    ↓
n8n generates AI content
    ↓
POST /api/method/dsi_catalogue.api.receive_generation_callback
    ↓
Content stored in Redis cache
    ↓
User sees preview, can regenerate
    ↓
User clicks "Publish"
    ↓
Website Item created/updated
```

---

## Key Features

### Cloudinary Image Support
- External Cloudinary URLs stored directly in Website Item
- `doc.flags.ignore_validate = True` bypasses Frappe file validation
- Website Slideshow supports external URLs

### Async Content Generation
- Task-based system using Redis cache
- Polling from frontend every 3 seconds
- 1-hour TTL for generated content
- Temperature control for AI creativity

### SEO Support
- Creates Website Route Meta for each Website Item
- Stores title, description, and keywords
- Integrates with AI-generated SEO content

### Field Truncation
- Image alt text truncated to 140 characters
- Slideshow headings/descriptions truncated to 140 characters
- Prevents Frappe field length validation errors

---

## File Structure

```
dsi_catalogue/
├── dsi_catalogue/
│   ├── __init__.py
│   ├── api.py                    # All whitelisted API functions
│   ├── hooks.py                  # App configuration
│   ├── dsi_catalogue/
│   │   ├── __init__.py
│   │   └── doctype/
│   │       ├── __init__.py
│   │       └── product_catalogue/
│   │           ├── __init__.py
│   │           ├── product_catalogue.json    # DocType definition
│   │           └── product_catalogue.py      # DocType controller
│   └── public/
│       ├── js/
│       │   └── item_publish.js   # Client-side modal scripts
│       └── css/
│           └── publish_modal.css # Modal styling
└── setup.py
```

---

## Troubleshooting

### Common Issues

1. **"All Images attached to Website Slideshow should be public"**
   - Fixed by `doc.flags.ignore_validate = True` on slideshow

2. **"Website Image cannot be found"**
   - Fixed by `doc.flags.ignore_validate = True` on Website Item

3. **"Value too big" / Field truncation errors**
   - Alt text and descriptions truncated to 140 characters

4. **Last synced shows wrong time**
   - Fixed by converting datetime to ISO format with 'Z' suffix for UTC

### Restarting Services

```bash
cd /home/ubuntu/frappe-bench
bench clear-cache
bench build --app dsi_catalogue
sudo supervisorctl restart all
```

---

## Version History

- **v1.0** - Initial release with Product Catalogue sync
- **v1.1** - Added async content generation with preview
- **v1.2** - Added Cloudinary support and validation bypasses
- **v1.3** - Fixed timezone issues and field truncation
