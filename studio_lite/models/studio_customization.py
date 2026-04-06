import json

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class StudioCustomization(models.Model):
    _name = 'studio.customization'
    _description = 'Studio Lite Customization'
    _order = 'create_date desc'

    name = fields.Char('Description', required=True)
    model_id = fields.Many2one(
        'ir.model', 'Model', required=True, ondelete='cascade', index=True,
    )
    model_name = fields.Char(related='model_id.model', store=True, string='Technical Model')
    customization_type = fields.Selection([
        ('field_create', 'Field Created'),
        ('field_hide', 'Field Hidden'),
        ('field_insert', 'Field Inserted'),
        ('field_property', 'Property Changed'),
    ], 'Type', required=True, index=True)
    field_id = fields.Many2one('ir.model.fields', 'Custom Field', ondelete='set null')
    view_id = fields.Many2one(
        'ir.ui.view', 'Generated View', ondelete='set null',
        help='The inherited view record created by this customization.',
    )
    target_view_id = fields.Many2one(
        'ir.ui.view', 'Target View', ondelete='set null',
        help='The original view that was customized.',
    )
    state = fields.Selection([
        ('active', 'Active'),
        ('reverted', 'Reverted'),
    ], default='active', required=True, index=True)
    user_id = fields.Many2one(
        'res.users', 'Applied By',
        default=lambda self: self.env.user, readonly=True,
    )
    technical_data = fields.Text('Technical Data', help='JSON payload for export.')

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def action_revert(self):
        """Revert one or more customizations."""
        for rec in self:
            if rec.state == 'reverted':
                raise UserError(_('Customization "%s" is already reverted.', rec.name))

            # Prevent reverting a field that is still used by other active custs
            if rec.customization_type == 'field_create' and rec.field_id:
                dependents = self.search([
                    ('field_id', '=', rec.field_id.id),
                    ('id', '!=', rec.id),
                    ('state', '=', 'active'),
                    ('customization_type', '!=', 'field_create'),
                ])
                if dependents:
                    raise UserError(_(
                        'Cannot revert: the field is still referenced by %d other '
                        'active customization(s). Revert those first.',
                        len(dependents),
                    ))

            # Remove generated view
            if rec.view_id and rec.view_id.exists():
                rec.view_id.sudo().unlink()

            # Remove custom field (drops DB column)
            if rec.customization_type == 'field_create' and rec.field_id and rec.field_id.exists():
                rec.field_id.sudo().unlink()

            rec.state = 'reverted'

    def action_open_target_view(self):
        """Open the target view form for debugging."""
        self.ensure_one()
        if not self.target_view_id:
            raise UserError(_('No target view recorded for this customization.'))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ir.ui.view',
            'res_id': self.target_view_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
