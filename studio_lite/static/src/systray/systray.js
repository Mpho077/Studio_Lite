/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

/**
 * Systray icon that opens the Studio Lite design panel.
 * Only visible when the user is on a form view.
 */
export class StudioLiteSystray extends Component {
    static template = "studio_lite.Systray";
    static props = [];

    setup() {
        this.action = useService("action");
        this.dialog = useService("dialog");
    }

    async onClick() {
        const controller = this.action.currentController;
        if (!controller) return;

        const action = controller.action;
        const resModel = action.res_model;
        const viewType = controller.view?.type;

        // Dynamically import the design panel to avoid circular deps
        const { StudioDesignPanel } = await odoo.loader.modules.get(
            "@studio_lite/design_panel/design_panel"
        ) || {};

        if (StudioDesignPanel) {
            // Track if any field change was applied during this session
            let hasChanges = false;

            this.dialog.add(
                StudioDesignPanel,
                {
                    resModel,
                    viewType: viewType || "form",
                    actionId: action.id,
                    onFieldChanged: () => { hasChanges = true; },
                },
                {
                    onClose: () => {
                        if (hasChanges) {
                            // Reload the current view so all changes are visible
                            window.location.reload();
                        }
                    },
                }
            );
        }
    }
}

export const studioLiteSystrayItem = {
    Component: StudioLiteSystray,
    isDisplayed(env) {
        const controller = env.services.action?.currentController;
        if (!controller) return false;
        const viewType = controller.view?.type;
        return viewType === "form" || viewType === "list";
    },
};

registry
    .category("systray")
    .add("studio_lite.design_button", studioLiteSystrayItem, { sequence: 50 });
