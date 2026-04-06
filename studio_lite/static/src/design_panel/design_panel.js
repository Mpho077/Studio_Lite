/** @odoo-module **/

import { Component, useState, onWillStart, onMounted, useRef } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";

/**
 * Design Panel — Full-screen dialog showing all fields on the current
 * model/view with toggle visibility, add custom field, and drag-to-reorder.
 */
export class StudioDesignPanel extends Component {
    static template = "studio_lite.DesignPanel";
    static components = { Dialog };
    static props = {
        resModel: { type: String },
        viewType: { type: String, optional: true },
        actionId: { optional: true },
        onFieldChanged: { type: Function, optional: true },
        close: { type: Function },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.fieldListRef = useRef("fieldList");

        this.state = useState({
            loading: true,
            fields: [],               // All fields on the model
            viewFields: [],            // Fields currently in the view
            hiddenFieldNames: [],      // Fields hidden by studio
            layout: null,             // Parsed view layout tree
            modelId: null,
            viewId: null,
            viewName: "",
            searchQuery: "",
            tab: "layout",           // "layout" | "hidden" | "available" | "create"
            // Create field form
            newFieldName: "",
            newFieldLabel: "",
            newFieldType: "char",
            newSelectionOptions: "",
            newRelationModel: "",
            saving: false,
            // Drag state
            dragIndex: null,
            // Layout tab — inline field picker
            pickerForXpath: null,
            pickerLabel: "",
            pickerSearch: "",
            // Layout tab — add section
            addingSection: false,
            newSectionTitle: "",
            // Layout tab — add tab
            addingTab: false,
            newTabTitle: "",
            // Layout tab — add column
            addingColumnFor: null,
            newColumnTitle: "",
            // Layout tab — rename column
            renamingColumnXpath: null,
            renamingColumnTitle: "",
            // Layout tab — rename section
            renamingSectionXpath: null,
            renamingSectionTitle: "",
            // Layout tab — rename tab/page
            renamingTabXpath: null,
            renamingTabTitle: "",
            // Layout tab — field properties inline editor
            expandedField: null,
            fieldAttrs: { string: "", required: false, readonly: false, placeholder: "", help: "" },
        });

        onWillStart(async () => {
            await this.loadData();
        });

        onMounted(() => {
            this.setupDragAndDrop();
        });
    }

    // -------------------------------------------------------------------------
    // Data Loading
    // -------------------------------------------------------------------------

    async loadData() {
        this.state.loading = true;
        try {
            const data = await rpc("/studio_lite/get_view_fields", {
                model: this.props.resModel,
                view_type: this.props.viewType || "form",
            });
            this.state.fields = data.all_fields || [];
            this.state.viewFields = data.view_fields || [];
            this.state.hiddenFieldNames = data.hidden_fields || [];
            this.state.modelId = data.model_id;
            this.state.viewId = data.view_id;
            this.state.viewName = data.view_name || "";
            this.state.layout = data.layout || null;
        } catch (e) {
            this.notification.add(_t("Failed to load view data."), { type: "danger" });
        }
        this.state.loading = false;
    }

    // -------------------------------------------------------------------------
    // Computed
    // -------------------------------------------------------------------------

    get currentFields() {
        const q = this.state.searchQuery.toLowerCase();
        return this.state.viewFields.filter(
            (f) =>
                !this.state.hiddenFieldNames.includes(f.name) &&
                (f.name.toLowerCase().includes(q) ||
                    (f.string || "").toLowerCase().includes(q))
        );
    }

    get hiddenFields() {
        const q = this.state.searchQuery.toLowerCase();
        return this.state.viewFields.filter(
            (f) =>
                this.state.hiddenFieldNames.includes(f.name) &&
                (f.name.toLowerCase().includes(q) ||
                    (f.string || "").toLowerCase().includes(q))
        );
    }

    get availableFields() {
        const inView = new Set(this.state.viewFields.map((f) => f.name));
        const q = this.state.searchQuery.toLowerCase();
        return this.state.fields.filter(
            (f) =>
                !inView.has(f.name) &&
                f.name !== "id" &&
                !f.name.startsWith("__") &&
                (f.name.toLowerCase().includes(q) ||
                    (f.string || "").toLowerCase().includes(q))
        );
    }

    get fieldTypes() {
        return [
            { value: "char", label: _t("Short Text") },
            { value: "text", label: _t("Long Text") },
            { value: "html", label: _t("Rich Text") },
            { value: "integer", label: _t("Whole Number") },
            { value: "float", label: _t("Decimal Number") },
            { value: "monetary", label: _t("Monetary") },
            { value: "boolean", label: _t("Checkbox") },
            { value: "date", label: _t("Date") },
            { value: "datetime", label: _t("Date & Time") },
            { value: "selection", label: _t("Dropdown") },
            { value: "many2one", label: _t("Many2one") },
            { value: "many2many", label: _t("Many2many / Tags") },
        ];
    }

    /** Flat list of items representing the view layout tree, used by the Layout tab. */
    get layoutItems() {
        const layout = this.state.layout;
        if (!layout) return [];
        const items = [];

        // Helper: push a field row (use 'kind' so spread of f doesn't clobber discriminator)
        const pushField = (f, depth, containerXpath) => {
            items.push({ kind: "field", id: containerXpath + "/" + f.name,
                depth, containerXpath, name: f.name, string: f.string, type: f.type,
                view_attrs: f.view_attrs || {} });
        };

        const pushGroup = (group, baseDepth) => {
            items.push({
                kind: "group_header", id: group.xpath, label: group.label || "Section",
                xpath: group.xpath, depth: baseDepth, hasColumns: group.columns.length > 0,
                fieldCount: group.columns.reduce((n, c) => n + c.fields.length, 0)
                            + group.fields.length,
            });
            if (group.columns.length > 0) {
                for (const col of group.columns) {
                    items.push({
                        kind: "column_header", id: col.xpath,
                        label: col.label || "Column", xpath: col.xpath,
                        depth: baseDepth + 1, fieldCount: col.fields.length,
                    });
                    for (const f of col.fields) pushField(f, baseDepth + 2, col.xpath);
                    items.push({ kind: "add_field_btn", id: col.xpath + "_add",
                        containerXpath: col.xpath,
                        containerLabel: (col.label || "Column"),
                        depth: baseDepth + 2 });
                }
            } else {
                for (const f of group.fields) pushField(f, baseDepth + 1, group.xpath);
                items.push({ kind: "add_field_btn", id: group.xpath + "_add",
                    containerXpath: group.xpath,
                    containerLabel: group.label || "Section",
                    depth: baseDepth + 1 });
            }
        };

        // Sheet-level groups
        for (const group of layout.groups || []) {
            pushGroup(group, 0);
        }
        // Add Section button
        items.push({ kind: "add_section_btn", id: "__add_section__", depth: 0 });

        // Notebook
        if (layout.notebook) {
            items.push({ kind: "notebook_header", id: "notebook", depth: 0,
                pageCount: layout.notebook.pages.length });
            for (const page of layout.notebook.pages) {
                items.push({ kind: "page_header", id: page.xpath, label: page.label,
                    xpath: page.xpath, invisible: page.invisible, depth: 1 });
                if (page.groups.length > 0) {
                    for (const group of page.groups) {
                        pushGroup(group, 2);
                    }
                } else {
                    for (const f of page.fields) pushField(f, 2, page.xpath);
                    items.push({ kind: "add_field_btn", id: page.xpath + "_add",
                        containerXpath: page.xpath, containerLabel: page.label,
                        depth: 2 });
                }
            }
            // Add Tab button
            items.push({ kind: "add_tab_btn", id: "__add_tab__", depth: 1 });
        }
        return items;
    }

    /** Fields shown in the layout inline picker (all model fields, search-filtered). */
    get pickerFields() {
        const q = (this.state.pickerSearch || "").toLowerCase();
        return this.state.fields.filter((f) => {
            if (f.name === "id" || f.name.startsWith("__")) return false;
            if (!q) return true;
            return f.name.toLowerCase().includes(q) ||
                   (f.string || "").toLowerCase().includes(q);
        });
    }

    // -------------------------------------------------------------------------
    // Actions
    // -------------------------------------------------------------------------

    setTab(tab) {
        this.state.tab = tab;
        this.state.searchQuery = "";
    }

    async toggleFieldVisibility(fieldName, hide) {
        this.state.saving = true;
        try {
            await rpc("/studio_lite/toggle_field_visibility", {
                model: this.props.resModel,
                view_type: this.props.viewType || "form",
                view_id: this.state.viewId,
                field_name: fieldName,
                hide: hide,
            });
            if (hide) {
                this.state.hiddenFieldNames.push(fieldName);
            } else {
                this.state.hiddenFieldNames = this.state.hiddenFieldNames.filter(
                    (n) => n !== fieldName
                );
            }
            this.props.onFieldChanged?.();
            this.notification.add(
                hide
                    ? _t('Field "%s" hidden.', fieldName)
                    : _t('Field "%s" shown.', fieldName),
                { type: "success" }
            );
        } catch (e) {
            this.notification.add(_t("Failed to update field visibility."), {
                type: "danger",
            });
        }
        this.state.saving = false;
    }

    async addFieldToView(fieldName) {
        this.state.saving = true;
        try {
            await rpc("/studio_lite/add_field_to_view", {
                model: this.props.resModel,
                view_type: this.props.viewType || "form",
                view_id: this.state.viewId,
                field_name: fieldName,
            });
            await this.loadData();
            this.props.onFieldChanged?.();
            this.notification.add(_t('Field "%s" added to view.', fieldName), {
                type: "success",
            });
        } catch (e) {
            this.notification.add(_t("Failed to add field to view."), {
                type: "danger",
            });
        }
        this.state.saving = false;
    }

    // -------------------------------------------------------------------------
    // Layout tab — inline picker
    // -------------------------------------------------------------------------

    openPicker(containerXpath, containerLabel) {
        this.state.pickerForXpath = containerXpath;
        this.state.pickerLabel = containerLabel || "container";
        this.state.pickerSearch = "";
    }

    closePicker() {
        this.state.pickerForXpath = null;
        this.state.pickerSearch = "";
    }

    async addToContainer(fieldName) {
        if (!this.state.pickerForXpath) return;
        const xpath = this.state.pickerForXpath;
        this.state.pickerForXpath = null;
        this.state.saving = true;
        try {
            await rpc("/studio_lite/add_field_to_container", {
                model: this.props.resModel,
                view_type: this.props.viewType || "form",
                view_id: this.state.viewId,
                field_name: fieldName,
                container_xpath: xpath,
            });
            await this.loadData();
            this.props.onFieldChanged?.();
            this.notification.add(_t('Field "%s" added.', fieldName), { type: "success" });
        } catch (e) {
            const msg = e.data?.message || e.message || "Unknown error";
            this.notification.add(msg, { type: "danger", sticky: true });
        }
        this.state.saving = false;
    }

    // -------------------------------------------------------------------------
    // Layout tab — add section / add tab
    // -------------------------------------------------------------------------

    async addSection() {
        const title = (this.state.newSectionTitle || "").trim();
        if (!title) {
            this.notification.add(_t("Please enter a section title."), { type: "warning" });
            return;
        }
        this.state.saving = true;
        try {
            await rpc("/studio_lite/add_section", {
                model: this.props.resModel,
                view_id: this.state.viewId,
                section_title: title,
            });
            this.state.addingSection = false;
            this.state.newSectionTitle = "";
            await this.loadData();
            this.props.onFieldChanged?.();
            this.notification.add(_t('Section "%s" added.', title), { type: "success" });
        } catch (e) {
            const msg = e.data?.message || e.message || "Unknown error";
            this.notification.add(msg, { type: "danger", sticky: true });
        }
        this.state.saving = false;
    }

    async addTab() {
        const title = (this.state.newTabTitle || "").trim();
        if (!title) {
            this.notification.add(_t("Please enter a tab title."), { type: "warning" });
            return;
        }
        this.state.saving = true;
        try {
            await rpc("/studio_lite/add_tab", {
                model: this.props.resModel,
                view_id: this.state.viewId,
                tab_title: title,
            });
            this.state.addingTab = false;
            this.state.newTabTitle = "";
            await this.loadData();
            this.props.onFieldChanged?.();
            this.notification.add(_t('Tab "%s" added.', title), { type: "success" });
        } catch (e) {
            const msg = e.data?.message || e.message || "Unknown error";
            this.notification.add(msg, { type: "danger", sticky: true });
        }
        this.state.saving = false;
    }

    async addColumn(groupXpath) {
        const title = (this.state.newColumnTitle || "").trim();
        this.state.addingColumnFor = null;
        this.state.newColumnTitle = "";
        this.state.saving = true;
        try {
            await rpc("/studio_lite/add_column", {
                model: this.props.resModel,
                view_id: this.state.viewId,
                group_xpath: groupXpath,
                column_title: title,
            });
            await this.loadData();
            this.props.onFieldChanged?.();
            this.notification.add(_t("Column added."), { type: "success" });
        } catch (e) {
            const msg = e.data?.message || e.message || "Unknown error";
            this.notification.add(msg, { type: "danger", sticky: true });
        }
        this.state.saving = false;
    }

    async deleteColumn(columnXpath) {
        if (!confirm(_t("Delete this column and all its fields from the view?"))) return;
        this.state.saving = true;
        try {
            await rpc("/studio_lite/delete_column", {
                model: this.props.resModel,
                view_id: this.state.viewId,
                column_xpath: columnXpath,
            });
            await this.loadData();
            this.props.onFieldChanged?.();
            this.notification.add(_t("Column removed."), { type: "success" });
        } catch (e) {
            const msg = e.data?.message || e.message || "Unknown error";
            this.notification.add(msg, { type: "danger", sticky: true });
        }
        this.state.saving = false;
    }

    async renameColumn(columnXpath) {
        const title = (this.state.renamingColumnTitle || "").trim();
        this.state.renamingColumnXpath = null;
        this.state.renamingColumnTitle = "";
        this.state.saving = true;
        try {
            await rpc("/studio_lite/rename_column", {
                model: this.props.resModel,
                view_id: this.state.viewId,
                column_xpath: columnXpath,
                new_title: title,
            });
            await this.loadData();
            this.props.onFieldChanged?.();
            this.notification.add(_t("Column renamed."), { type: "success" });
        } catch (e) {
            const msg = e.data?.message || e.message || "Unknown error";
            this.notification.add(msg, { type: "danger", sticky: true });
        }
        this.state.saving = false;
    }

    async removeFieldFromView(fieldName) {
        if (!confirm(_t('Remove "%s" from this view?', fieldName))) return;
        this.state.saving = true;
        try {
            await rpc("/studio_lite/remove_field_from_view", {
                model: this.props.resModel,
                view_id: this.state.viewId,
                field_name: fieldName,
            });
            await this.loadData();
            this.props.onFieldChanged?.();
            this.notification.add(_t('Field "%s" removed from view.', fieldName), { type: "success" });
        } catch (e) {
            const msg = e.data?.message || e.message || "Unknown error";
            this.notification.add(msg, { type: "danger", sticky: true });
        }
        this.state.saving = false;
    }

    // -------------------------------------------------------------------------
    // Layout tab — field properties inline editor
    // -------------------------------------------------------------------------

    toggleFieldExpand(fieldName, item) {
        if (this.state.expandedField === fieldName) {
            this.state.expandedField = null;
        } else {
            this.state.expandedField = fieldName;
            const va = item.view_attrs || {};
            this.state.fieldAttrs = {
                string: va.string || "",
                required: va.required === "1" || va.required === "True",
                readonly: va.readonly === "1" || va.readonly === "True",
                placeholder: va.placeholder || "",
                help: va.help || "",
            };
        }
    }

    async updateFieldAttrs(fieldName) {
        this.state.expandedField = null;
        this.state.saving = true;
        const attrs = {};
        if (this.state.fieldAttrs.string) attrs.string = this.state.fieldAttrs.string;
        if (this.state.fieldAttrs.required) attrs.required = "1";
        if (this.state.fieldAttrs.readonly) attrs.readonly = "1";
        if (this.state.fieldAttrs.placeholder) attrs.placeholder = this.state.fieldAttrs.placeholder;
        if (this.state.fieldAttrs.help) attrs.help = this.state.fieldAttrs.help;
        try {
            await rpc("/studio_lite/update_field_attrs", {
                model: this.props.resModel,
                view_id: this.state.viewId,
                field_name: fieldName,
                attrs,
            });
            await this.loadData();
            this.props.onFieldChanged?.();
            this.notification.add(_t("Field properties updated."), { type: "success" });
        } catch (e) {
            const msg = e.data?.message || e.message || "Unknown error";
            this.notification.add(msg, { type: "danger", sticky: true });
        }
        this.state.saving = false;
    }

    // -------------------------------------------------------------------------
    // Layout tab — rename section / rename tab
    // -------------------------------------------------------------------------

    async renameSection(sectionXpath) {
        const title = (this.state.renamingSectionTitle || "").trim();
        this.state.renamingSectionXpath = null;
        this.state.renamingSectionTitle = "";
        this.state.saving = true;
        try {
            await rpc("/studio_lite/rename_section", {
                model: this.props.resModel,
                view_id: this.state.viewId,
                section_xpath: sectionXpath,
                new_title: title,
            });
            await this.loadData();
            this.props.onFieldChanged?.();
            this.notification.add(_t("Section renamed."), { type: "success" });
        } catch (e) {
            const msg = e.data?.message || e.message || "Unknown error";
            this.notification.add(msg, { type: "danger", sticky: true });
        }
        this.state.saving = false;
    }

    async renameTab(tabXpath) {
        const title = (this.state.renamingTabTitle || "").trim();
        this.state.renamingTabXpath = null;
        this.state.renamingTabTitle = "";
        this.state.saving = true;
        try {
            await rpc("/studio_lite/rename_tab", {
                model: this.props.resModel,
                view_id: this.state.viewId,
                tab_xpath: tabXpath,
                new_title: title,
            });
            await this.loadData();
            this.props.onFieldChanged?.();
            this.notification.add(_t("Tab renamed."), { type: "success" });
        } catch (e) {
            const msg = e.data?.message || e.message || "Unknown error";
            this.notification.add(msg, { type: "danger", sticky: true });
        }
        this.state.saving = false;
    }

    // -------------------------------------------------------------------------
    // Undo last Studio Lite action
    // -------------------------------------------------------------------------

    async undoLast() {
        if (!confirm(_t("Undo the last Studio Lite change on this model?"))) return;
        this.state.saving = true;
        try {
            const result = await rpc("/studio_lite/undo_last", {
                model: this.props.resModel,
                view_id: this.state.viewId,
            });
            if (result.success) {
                await this.loadData();
                this.props.onFieldChanged?.();
                this.notification.add(
                    _t('Undone: "%s"', result.undone || "last action"),
                    { type: "success" }
                );
            } else {
                this.notification.add(
                    result.message || _t("Nothing to undo."),
                    { type: "warning" }
                );
            }
        } catch (e) {
            const msg = e.data?.message || e.message || "Unknown error";
            this.notification.add(msg, { type: "danger", sticky: true });
        }
        this.state.saving = false;
    }

    async createAndAddField() {
        if (!this.state.newFieldLabel || !this.state.newFieldName) {
            this.notification.add(_t("Please fill in field name and label."), {
                type: "warning",
            });
            return;
        }
        this.state.saving = true;
        try {
            await rpc("/studio_lite/create_field", {
                model: this.props.resModel,
                view_type: this.props.viewType || "form",
                view_id: this.state.viewId,
                field_name: this.state.newFieldName,
                field_label: this.state.newFieldLabel,
                field_type: this.state.newFieldType,
                selection_options: this.state.newSelectionOptions,
                relation_model: this.state.newRelationModel,
            });
            this.state.newFieldName = "";
            this.state.newFieldLabel = "";
            this.state.newFieldType = "char";
            this.state.newSelectionOptions = "";
            this.state.newRelationModel = "";
            await this.loadData();
            this.props.onFieldChanged?.();
            this.notification.add(_t("Custom field created and added to view!"), {
                type: "success",
            });
            this.state.tab = "current";
        } catch (e) {
            const msg = e.data?.message || e.message || "Unknown error";
            this.notification.add(msg, { type: "danger", sticky: true });
        }
        this.state.saving = false;
    }

    async moveField(fieldName, direction) {
        this.state.saving = true;
        try {
            await rpc("/studio_lite/move_field", {
                model: this.props.resModel,
                view_type: this.props.viewType || "form",
                view_id: this.state.viewId,
                field_name: fieldName,
                direction: direction,
            });
            await this.loadData();
            this.props.onFieldChanged?.();
        } catch (e) {
            this.notification.add(_t("Failed to move field."), { type: "danger" });
        }
        this.state.saving = false;
    }

    // -------------------------------------------------------------------------
    // Drag and Drop
    // -------------------------------------------------------------------------

    setupDragAndDrop() {
        // Will be set up via template event handlers
    }

    onDragStart(index, ev) {
        this.state.dragIndex = index;
        ev.dataTransfer.effectAllowed = "move";
        ev.target.classList.add("studio-dragging");
    }

    onDragOver(index, ev) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
    }

    async onDrop(targetIndex, ev) {
        ev.preventDefault();
        const sourceIndex = this.state.dragIndex;
        if (sourceIndex === null || sourceIndex === targetIndex) return;

        const fields = [...this.currentFields];
        const sourceField = fields[sourceIndex];
        const targetField = fields[targetIndex];

        if (!sourceField || !targetField) return;

        this.state.saving = true;
        try {
            await rpc("/studio_lite/reorder_field", {
                model: this.props.resModel,
                view_type: this.props.viewType || "form",
                view_id: this.state.viewId,
                field_name: sourceField.name,
                target_field: targetField.name,
                position: sourceIndex < targetIndex ? "after" : "before",
            });
            await this.loadData();
            this.props.onFieldChanged?.();
        } catch (e) {
            this.notification.add(_t("Failed to reorder field."), {
                type: "danger",
            });
        }
        this.state.dragIndex = null;
        this.state.saving = false;
    }

    onDragEnd(ev) {
        ev.target.classList.remove("studio-dragging");
        this.state.dragIndex = null;
    }

    getFieldTypeIcon(ttype) {
        const icons = {
            char: "fa-font",
            text: "fa-align-left",
            html: "fa-code",
            integer: "fa-hashtag",
            float: "fa-calculator",
            monetary: "fa-money",
            boolean: "fa-check-square-o",
            date: "fa-calendar",
            datetime: "fa-calendar-check-o",
            binary: "fa-file",
            selection: "fa-list",
            many2one: "fa-arrow-right",
            one2many: "fa-arrows",
            many2many: "fa-tags",
        };
        return icons[ttype] || "fa-question";
    }
}
