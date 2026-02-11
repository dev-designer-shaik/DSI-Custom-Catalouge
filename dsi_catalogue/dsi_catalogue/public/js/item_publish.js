// DSI Product Catalogue - Publish to Website Modal
// Redesigned: Folder selection -> Generate -> Preview -> Publish

frappe.ui.form.on("Item", {
  refresh(frm) {
    // Remove the existing webshop button if present
    frm.remove_custom_button(__("Publish in Website"), __("Actions"));

    // Add our enhanced publish button
    if (!frm.doc.__islocal) {
      frm.add_custom_button(__("Publish to Website"), function() {
        new PublishToWebsiteModal(frm);
      }, __("Actions"));

      // Make button more prominent
      frm.change_custom_button_type(__("Publish to Website"), __("Actions"), "primary");
    }
  }
});

class PublishToWebsiteModal {
  constructor(frm) {
    this.frm = frm;
    this.selectedFolder = null;
    this.previewData = null;
    this.pollingInterval = null;
    this.init();
  }

  init() {
    this.dialog = new frappe.ui.Dialog({
      title: __("Select Product from Catalogue"),
      size: "extra-large",
      fields: this.getFields(),
      primary_action_label: __("Generate Content"),
      primary_action: () => this.generateContent()
    });

    this.loadFolderTree();
    this.dialog.show();
  }

  getFields() {
    return [
      {
        fieldtype: "HTML",
        fieldname: "main_container",
        options: this.getContainerHTML()
      }
    ];
  }

  getContainerHTML() {
    return `
      <div class="publish-modal-container" style="display: flex; gap: 20px; min-height: 400px;">
        <div class="folder-tree-panel" style="flex: 1; border: 1px solid var(--border-color); border-radius: 8px; padding: 15px; overflow-y: auto; max-height: 400px;">
          <div class="tree-header" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
            <h6 style="margin: 0;">Product Catalogue</h6>
            <div style="display: flex; align-items: center; gap: 10px;">
              <span id="last-synced-time" class="text-muted" style="font-size: 11px;"></span>
              <button class="btn btn-xs btn-default sync-btn" onclick="window.publishModal && window.publishModal.syncCatalogue()">
                <i class="fa fa-refresh"></i> Sync
              </button>
            </div>
          </div>
          <div id="folder-tree-container" style="font-size: 13px;">
            <div class="text-muted">Loading catalogue...</div>
          </div>
        </div>
        <div class="preview-panel" style="flex: 1; border: 1px solid var(--border-color); border-radius: 8px; padding: 15px;">
          <h6>Folder Preview</h6>
          <div id="preview-container">
            <div class="text-muted">Select a product folder to see preview</div>
          </div>
        </div>
      </div>
    `;
  }

  async loadFolderTree() {
    window.publishModal = this;

    try {
      // Force fresh data - add timestamp to bypass any caching
      const response = await frappe.call({
        method: "dsi_catalogue.api.get_product_catalogue_tree",
        args: {},
        async: true,
        freeze: false
      });

      this.folderTree = response.message || [];

      if (this.folderTree.length === 0) {
        const container = this.dialog.$wrapper.find("#folder-tree-container");
        container.html(`
          <div class="text-muted">
            <p>No catalogue data found.</p>
            <p>Click <strong>Sync</strong> to load the product catalogue from the repository.</p>
          </div>
        `);
        return;
      }

      this.renderFolderTree(this.folderTree);
    } catch (error) {
      console.error("Error loading folder tree:", error);
      const container = this.dialog.$wrapper.find("#folder-tree-container");
      container.html(`<div class="text-danger">Error loading catalogue: ${error.message || "Unknown error"}</div>`);
    }
  }

  renderFolderTree(treeData) {
    const container = this.dialog.$wrapper.find("#folder-tree-container");
    container.html(this.buildTreeHTML(treeData));

    // Show last synced time from first node with data
    const findLastSynced = (nodes) => {
      for (const node of nodes) {
        if (node.last_synced) return node.last_synced;
        if (node.children && node.children.length) {
          const found = findLastSynced(node.children);
          if (found) return found;
        }
      }
      return null;
    };
    const lastSynced = findLastSynced(treeData);
    if (lastSynced) {
      this.dialog.$wrapper.find("#last-synced-time").text("Synced: " + frappe.datetime.prettyDate(lastSynced));
    }

    // Add click handlers for folders
    container.find(".folder-item").on("click", (e) => {
      e.stopPropagation();
      const folderId = $(e.currentTarget).data("folder-id");
      this.selectFolder(folderId);
    });

    // Add toggle handlers for expand/collapse
    container.find(".folder-toggle").on("click", (e) => {
      e.stopPropagation();
      const $toggle = $(e.currentTarget);
      const $children = $toggle.parent().siblings(".folder-children");
      $children.toggle();
      $toggle.find("i").toggleClass("fa-chevron-right fa-chevron-down");
    });
  }

  buildTreeHTML(nodes, level = 0) {
    if (!nodes || nodes.length === 0) return "";

    let html = `<ul class="folder-tree" style="list-style: none; padding-left: ${level * 15}px; margin: 0;">`;

    for (const node of nodes) {
      const hasChildren = node.children && node.children.length > 0;
      const indexKeyBadge = node.index_key ?
        `<span class="badge badge-light" style="font-size: 10px; margin-left: 5px;">${node.index_key}</span>` : "";

      html += `
        <li style="margin: 2px 0;">
          <div class="folder-item" data-folder-id="${node.name}"
               style="display: flex; align-items: center; padding: 5px 8px; border-radius: 4px; cursor: pointer;">
            ${hasChildren ?
              `<span class="folder-toggle" style="width: 16px; cursor: pointer;">
                <i class="fa fa-chevron-right" style="font-size: 10px;"></i>
              </span>` :
              `<span style="width: 16px;"></span>`
            }
            <i class="fa fa-folder" style="color: #f0c14b; margin-right: 8px;"></i>
            <span class="folder-name">${node.display_name || node.name}</span>
            ${indexKeyBadge}
          </div>
          ${hasChildren ? `<div class="folder-children" style="display: none;">${this.buildTreeHTML(node.children, level + 1)}</div>` : ""}
        </li>
      `;
    }

    html += "</ul>";
    return html;
  }

  async selectFolder(folderId) {
    this.selectedFolder = folderId;

    // Highlight selection
    this.dialog.$wrapper.find(".folder-item").css("background-color", "transparent");
    this.dialog.$wrapper.find(`[data-folder-id="${folderId}"]`).css("background-color", "var(--bg-light-gray)");

    // Load preview
    try {
      const response = await frappe.call({
        method: "dsi_catalogue.api.get_folder_preview",
        args: { folder_id: folderId }
      });

      this.previewData = response.message;
      this.renderPreview();
    } catch (error) {
      console.error("Error loading preview:", error);
      const container = this.dialog.$wrapper.find("#preview-container");
      container.html(`<div class="text-danger">Error loading preview: ${error.message}</div>`);
    }
  }

  renderPreview() {
    const container = this.dialog.$wrapper.find("#preview-container");
    const data = this.previewData;

    if (!data) {
      container.html("<div class=\"text-muted\">No preview data available</div>");
      return;
    }

    const imagesHTML = data.images && data.images.length > 0 ?
      data.images.map(img =>
        `<img src="${img.thumbnail || img.url || img.web_optimized}"
              style="width: 60px; height: 60px; object-fit: cover; border-radius: 4px; margin: 2px;"
              onerror="this.style.display='none'">`
      ).join("") :
      "<span class=\"text-muted\">No images</span>";

    const aiAnalysis = data.aiAnalysis || {};

    container.html(`
      <div class="preview-content">
        ${data.heroImage ?
          `<img src="${data.heroImage}" style="width: 100%; max-height: 150px; object-fit: cover; border-radius: 8px; margin-bottom: 15px;" onerror="this.style.display='none'">` :
          ""}

        <h5 style="margin-bottom: 5px;">${data.productName || "Unknown Product"}</h5>
        <p class="text-muted" style="margin-bottom: 10px;">
          <strong>Index Key:</strong> ${data.indexKey || "N/A"}
        </p>

        ${data.palace ? `<p><strong>Palace:</strong> ${data.palace}</p>` : ""}
        ${data.productRange ? `<p><strong>Range:</strong> ${data.productRange}</p>` : ""}

        <div style="margin: 15px 0;">
          <strong>Images Found:</strong> ${data.imageCount || 0}
          <div style="display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px;">
            ${imagesHTML}
          </div>
        </div>

        ${Object.keys(aiAnalysis).length > 0 ? `
          <div class="ai-analysis" style="background: var(--bg-light-gray); padding: 10px; border-radius: 6px; margin-top: 10px;">
            <strong>AI Analysis</strong>
            ${aiAnalysis.brandConsistency ? `<p style="margin: 5px 0;"><strong>Luxury Score:</strong> ${aiAnalysis.brandConsistency}/10</p>` : ""}
            ${aiAnalysis.technicalQuality?.overallScore ? `<p style="margin: 5px 0;"><strong>Quality:</strong> ${aiAnalysis.technicalQuality.overallScore}/10</p>` : ""}
          </div>
        ` : ""}
      </div>
    `);
  }

  async syncCatalogue() {
    frappe.show_alert({
      message: __("Refreshing catalogue data..."), 
      indicator: "blue"
    }, 3);

    // Reload the folder tree to get fresh data from database
    await this.loadFolderTree();

    frappe.show_alert({
      message: __("Catalogue refreshed!"), 
      indicator: "green"
    }, 3);
  }





  showLoadingState() {
    const previewContainer = this.dialog.$wrapper.find("#preview-container");
    previewContainer.html(`
      <div class="loading-state" style="text-align: center; padding: 40px 20px;">
        <div class="spinner" style="margin-bottom: 20px;">
          <svg width="50" height="50" viewBox="0 0 50 50" style="animation: spin 1s linear infinite;">
            <circle cx="25" cy="25" r="20" fill="none" stroke="var(--primary)" stroke-width="4" stroke-dasharray="80 40" stroke-linecap="round"/>
          </svg>
        </div>
        <h5 style="margin-bottom: 10px; color: var(--text-color);">Generating Content...</h5>
        <p class="text-muted" style="margin: 0;">AI is analyzing images and creating product descriptions.</p>
        <p class="text-muted" style="margin: 5px 0 0 0; font-size: 12px;">This may take up to 2 minutes.</p>
      </div>
      <style>
        @keyframes spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      </style>
    `);
  }

  hideLoadingState() {
    // Restore preview if we have data, otherwise show default message
    if (this.previewData) {
      this.renderPreview();
    } else {
      const previewContainer = this.dialog.$wrapper.find("#preview-container");
      previewContainer.html("<div class=\"text-muted\">Select a product folder to see preview</div>");
    }
  }

  async generateContent() {
    if (!this.selectedFolder) {
      frappe.msgprint(__("Please select a product folder first"));
      return;
    }

    // Show "Working on it" toast
    frappe.show_alert({message: __("Working on it..."), indicator: "blue"}, 5);

    // Disable the button and show loading state
    this.dialog.get_primary_btn().prop("disabled", true).text(__("Generating..."));
    this.showLoadingState();

    try {
      const response = await frappe.call({
        method: "dsi_catalogue.api.start_content_generation",
        args: {
          folder_id: this.selectedFolder,
          item_code: this.frm.doc.item_code,
          temperature: 0.7
        }
      });

      if (response.message && response.message.success) {
        const taskId = response.message.task_id;
        // Start polling for completion
        this.pollForCompletion(taskId);
      } else {
        frappe.msgprint(__("Failed to start content generation: ") + (response.message?.error || "Unknown error"));
        this.dialog.get_primary_btn().prop("disabled", false).text(__("Generate Content"));
        this.hideLoadingState();
      }
    } catch (error) {
      console.error("Error starting generation:", error);
      frappe.msgprint(__("Error starting content generation: ") + (error.message || error));
      this.dialog.get_primary_btn().prop("disabled", false).text(__("Generate Content"));
      this.hideLoadingState();
    }
  }

  pollForCompletion(taskId) {
    let pollCount = 0;
    const maxPolls = 60; // Max 3 minutes (60 * 3 seconds)

    this.pollingInterval = setInterval(async () => {
      pollCount++;

      if (pollCount > maxPolls) {
        clearInterval(this.pollingInterval);
        this.pollingInterval = null;
        frappe.msgprint(__("Content generation timed out. Please try again."));
        this.dialog.get_primary_btn().prop("disabled", false).text(__("Generate Content"));
        this.hideLoadingState();
        return;
      }

      try {
        const status = await frappe.call({
          method: "dsi_catalogue.api.get_generation_status",
          args: { task_id: taskId }
        });

        const data = status.message;

        if (data.status === "completed") {
          clearInterval(this.pollingInterval);
          this.pollingInterval = null;
          this.dialog.hide();

          // Show preview modal with generated content
          new ContentPreviewModal(this.frm, data, this.selectedFolder);

        } else if (data.status === "error") {
          clearInterval(this.pollingInterval);
          this.pollingInterval = null;
          frappe.msgprint(__("Content generation failed: ") + (data.error || "Unknown error"));
          this.dialog.get_primary_btn().prop("disabled", false).text(__("Generate Content"));
          this.hideLoadingState();

        } else if (data.status === "not_found") {
          clearInterval(this.pollingInterval);
          this.pollingInterval = null;
          frappe.msgprint(__("Task not found or expired. Please try again."));
          this.dialog.get_primary_btn().prop("disabled", false).text(__("Generate Content"));
          this.hideLoadingState();
        }
        // Otherwise keep polling (status === "processing")

      } catch (error) {
        console.error("Error polling for status:", error);
        // Continue polling despite errors
      }
    }, 3000); // Poll every 3 seconds
  }
}


class ContentPreviewModal {
  constructor(frm, generatedData, folderId) {
    this.frm = frm;
    this.data = generatedData;
    this.folderId = folderId;
    this.temperature = 0.7;
    this.init();
  }

  init() {
    this.dialog = new frappe.ui.Dialog({
      title: __("Preview Generated Content"),
      size: "extra-large",
      fields: this.getFields(),
      primary_action_label: __("Publish to Website"),
      primary_action: () => this.publish(),
      secondary_action_label: __("Regenerate"),
      secondary_action: () => this.regenerate()
    });

    this.renderContent();
    this.dialog.show();
  }

  getFields() {
    return [
      {
        fieldtype: "HTML",
        fieldname: "preview_content"
      },
      {
        fieldtype: "Section Break",
        label: __("Regeneration Options"),
        collapsible: 1,
        collapsible_depends_on: "eval:false"
      },
      {
        fieldtype: "Float",
        fieldname: "temperature",
        label: __("AI Temperature"),
        default: 0.7,
        description: __("0.0 = More conservative/consistent, 1.0 = More creative/varied")
      }
    ];
  }

  renderContent() {
    const wrapper = this.dialog.fields_dict.preview_content.$wrapper;
    const content = this.data.content || {};
    const images = this.data.images || [];
    const seoData = this.data.seo_data || {};
    const specs = content.specifications || {};
    const imageAlts = content.image_alts || {};

    const specsHTML = Object.keys(specs).length > 0 ?
      `<table class="table table-bordered" style="font-size: 13px;">
        <tbody>
          ${Object.entries(specs).map(([label, value]) =>
            `<tr><td style="font-weight: 500; width: 40%;">${this.escapeHtml(label)}</td><td>${this.escapeHtml(String(value))}</td></tr>`
          ).join("")}
        </tbody>
      </table>` :
      "<p class=\"text-muted\">No specifications generated</p>";

    const keywordsHTML = seoData.keywords && seoData.keywords.length > 0 ?
      seoData.keywords.map(kw =>
        `<span class="badge badge-secondary" style="margin: 2px;">${this.escapeHtml(kw)}</span>`
      ).join("") :
      "<span class=\"text-muted\">No keywords</span>";

    const imagesWithAltsHTML = images.length > 0 ?
      `<div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 15px;">
        ${images.map(img => {
          const altText = imageAlts[img.fileName] || img.description || "";
          return `
            <div style="border: 1px solid var(--border-color); border-radius: 8px; padding: 10px;">
              <img src="${img.cloudinaryUrl || img.url}"
                   style="width: 100%; height: 120px; object-fit: cover; border-radius: 4px; margin-bottom: 8px;"
                   onerror="this.style.display='none'">
              <div style="font-size: 11px; color: var(--text-muted);">${this.escapeHtml(img.fileName || "")}</div>
              <div style="font-size: 12px; margin-top: 4px;">${this.escapeHtml(altText) || "<em class='text-muted'>No alt text</em>"}</div>
            </div>
          `;
        }).join("")}
      </div>` :
      "<p class=\"text-muted\">No images</p>";

    wrapper.html(`
      <div class="preview-container" style="max-height: 500px; overflow-y: auto; padding-right: 10px;">

        <!-- Product Name & Description -->
        <div class="preview-section" style="margin-bottom: 20px;">
          <h5 style="color: var(--primary); margin-bottom: 10px;">Product Information</h5>
          <div style="background: var(--bg-light-gray); padding: 15px; border-radius: 8px;">
            <h4 style="margin: 0 0 10px 0;">${this.escapeHtml(content.product_name || "Untitled Product")}</h4>
            <p style="margin: 0; font-style: italic; color: var(--text-muted);">${this.escapeHtml(content.description || "")}</p>
          </div>
        </div>

        <!-- Product Details -->
        <div class="preview-section" style="margin-bottom: 20px;">
          <h5 style="color: var(--primary); margin-bottom: 10px;">Product Details</h5>
          <div style="background: var(--bg-light-gray); padding: 15px; border-radius: 8px;">
            ${content.product_details || "<p class=\"text-muted\">No product details generated</p>"}
          </div>
        </div>

        <!-- Specifications -->
        <div class="preview-section" style="margin-bottom: 20px;">
          <h5 style="color: var(--primary); margin-bottom: 10px;">Specifications</h5>
          ${specsHTML}
        </div>

        <!-- Care Instructions -->
        ${content.care_instructions ? `
          <div class="preview-section" style="margin-bottom: 20px;">
            <h5 style="color: var(--primary); margin-bottom: 10px;">Care Instructions</h5>
            <div style="background: var(--bg-light-gray); padding: 15px; border-radius: 8px;">
              <p style="margin: 0;">${this.escapeHtml(content.care_instructions)}</p>
            </div>
          </div>
        ` : ""}

        <!-- SEO -->
        <div class="preview-section" style="margin-bottom: 20px;">
          <h5 style="color: var(--primary); margin-bottom: 10px;">SEO Metadata</h5>
          <div style="background: var(--bg-light-gray); padding: 15px; border-radius: 8px;">
            <p style="margin: 0 0 8px 0;"><strong>Title:</strong> ${this.escapeHtml(seoData.title || content.seo_title || "")}</p>
            <p style="margin: 0 0 8px 0;"><strong>Description:</strong> ${this.escapeHtml(seoData.description || content.seo_description || "")}</p>
            <p style="margin: 0;"><strong>Keywords:</strong> ${keywordsHTML}</p>
          </div>
        </div>

        <!-- Images with Alt Texts -->
        <div class="preview-section" style="margin-bottom: 20px;">
          <h5 style="color: var(--primary); margin-bottom: 10px;">Images & Alt Texts</h5>
          ${imagesWithAltsHTML}
        </div>

      </div>
    `);
  }

  escapeHtml(text) {
    if (!text) return "";
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  async regenerate() {
    this.temperature = this.dialog.get_value("temperature") || 0.7;
    this.dialog.hide();

    // Show working toast
    frappe.show_alert({message: __("Regenerating content..."), indicator: "blue"}, 5);

    try {
      const response = await frappe.call({
        method: "dsi_catalogue.api.start_content_generation",
        args: {
          folder_id: this.folderId,
          item_code: this.frm.doc.item_code,
          temperature: this.temperature
        }
      });

      if (response.message && response.message.success) {
        const taskId = response.message.task_id;
        this.pollForRegeneration(taskId);
      } else {
        frappe.msgprint(__("Failed to start regeneration: ") + (response.message?.error || "Unknown error"));
      }
    } catch (error) {
      console.error("Error starting regeneration:", error);
      frappe.msgprint(__("Error starting regeneration: ") + (error.message || error));
    }
  }

  pollForRegeneration(taskId) {
    let pollCount = 0;
    const maxPolls = 60;

    const interval = setInterval(async () => {
      pollCount++;

      if (pollCount > maxPolls) {
        clearInterval(interval);
        frappe.msgprint(__("Regeneration timed out. Please try again."));
        return;
      }

      try {
        const status = await frappe.call({
          method: "dsi_catalogue.api.get_generation_status",
          args: { task_id: taskId }
        });

        const data = status.message;

        if (data.status === "completed") {
          clearInterval(interval);
          // Show new preview modal with regenerated content
          new ContentPreviewModal(this.frm, data, this.folderId);

        } else if (data.status === "error") {
          clearInterval(interval);
          frappe.msgprint(__("Regeneration failed: ") + (data.error || "Unknown error"));

        } else if (data.status === "not_found") {
          clearInterval(interval);
          frappe.msgprint(__("Task not found or expired. Please try again."));
        }
      } catch (error) {
        console.error("Error polling for regeneration:", error);
      }
    }, 3000);
  }

  async publish() {
    const content = this.data.content || {};
    const images = this.data.images || [];
    const seoData = this.data.seo_data || {};
    const itemGroupData = this.data.item_group_data || {};

    this.dialog.hide();

    try {
      const response = await frappe.call({
        method: "dsi_catalogue.api.publish_website_item",
        args: {
          folder_id: this.folderId,
          item_code: this.frm.doc.item_code,
          content: JSON.stringify(content),
          images: JSON.stringify(images),
          seo_data: JSON.stringify(seoData),
          item_group_data: JSON.stringify(itemGroupData)
        },
        freeze: true,
        freeze_message: __("Publishing to website...")
      });

      if (response.message && response.message.success) {
        frappe.show_alert({
          message: response.message.created ? __("Website Item created!") : __("Website Item updated!"),
          indicator: "green"
        }, 5);
        this.frm.reload_doc();
      } else {
        frappe.msgprint({
          title: __("Publish Failed"),
          message: response.message?.error || __("Unknown error occurred"),
          indicator: "red"
        });
      }
    } catch (error) {
      console.error("Error publishing:", error);
      frappe.msgprint({
        title: __("Publish Error"),
        message: error.message || __("Failed to publish to website"),
        indicator: "red"
      });
    }
  }
}
