import re
import json

from lxml import etree

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class StudioFieldVisibility(models.TransientModel):
    _name = 'studio.field.visibility'
    _description = 'Studio Lite — Hide Fields'

    model_id = fields.Many2one(
        'ir.model', 'Model', required=True,
        domain=[('transient', '=', False)],
    )
    model_name = fields.Char(compute='_compute_model_name')
    view_type = fields.Selection([
        ('form', 'Form View'),
        ('list', 'List View'),
    ], 'View Type', default='form', required=True)
    view_id = fields.Many2one(
        'ir.ui.view', 'Target View', required=True,
        domain="[('model', '=', model_name), ('type', '=', view_type), "
               "('inherit_id', '=', False)]",
    )
    field_ids = fields.Many2many(
        'ir.model.fields', string='Fields to Hide', required=True,
        domain="[('model_id', '=', model_id), ('name', '!=', 'id')]",
    )

    @api.depends('model_id')
    def _compute_model_name(self):
        for rec in self:
            rec.model_name = rec.model_id.model if rec.model_id else False

    def action_hide_fields(self):
        self.ensure_one()
        if not self.field_ids:
            raise UserError(_('Please select at least one field to hide.'))

        data = etree.Element('data')
        hidden_names = []

        for field in self.field_ids:
            xpath = etree.SubElement(data, 'xpath')
            xpath.set('expr', "//field[@name='%s']" % field.name)
            xpath.set('position', 'attributes')

            if self.view_type == 'list':
                attr = etree.SubElement(xpath, 'attribute')
                attr.set('name', 'column_invisible')
                attr.text = 'True'
            else:
                attr = etree.SubElement(xpath, 'attribute')
                attr.set('name', 'invisible')
                attr.text = '1'

            hidden_names.append(field.name)

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)

        view = self.env['ir.ui.view'].sudo().create({
            'name': 'studio_lite.hide.%s.%s' % (
                self.model_id.model.replace('.', '_'),
                '_'.join(hidden_names[:3]),
            ),
            'model': self.model_id.model,
            'inherit_id': self.view_id.id,
            'arch_db': arch,
            'priority': 9999,
        })

        tech_data = {
            'type': 'field_hide',
            'model': self.model_id.model,
            'view_type': self.view_type,
            'target_view_id': self.view_id.id,
            'target_view_xmlid': self.view_id.get_external_id().get(self.view_id.id) or '',
            'hidden_fields': hidden_names,
            'arch': arch,
        }

        self.env['studio.customization'].sudo().create({
            'name': _('Hidden %(count)d field(s) on %(model)s %(vtype)s view',
                      count=len(hidden_names), model=self.model_id.name,
                      vtype=self.view_type),
            'model_id': self.model_id.id,
            'customization_type': 'field_hide',
            'view_id': view.id,
            'target_view_id': self.view_id.id,
            'technical_data': json.dumps(tech_data, indent=2),
        })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Fields Hidden'),
                'message': _('%d field(s) hidden successfully.', len(hidden_names)),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
