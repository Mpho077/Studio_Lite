import re
import json

from lxml import etree

from odoo import models, fields, api, _
from odoo.exceptions import UserError

SAFE_NAME_RE = re.compile(r'^[a-z_][a-z0-9_]*$')


class StudioFieldInserter(models.TransientModel):
    _name = 'studio.field.inserter'
    _description = 'Studio Lite — Insert Field in View'

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
    field_id = fields.Many2one(
        'ir.model.fields', 'Field to Insert', required=True,
        domain="[('model_id', '=', model_id)]",
    )
    position = fields.Selection([
        ('after', 'After a Field'),
        ('before', 'Before a Field'),
        ('sheet_end', 'End of Form (new group)'),
        ('notebook_page', 'New Notebook Page'),
    ], 'Position', default='after', required=True)
    reference_field = fields.Char(
        'Reference Field',
        help='Technical name of the field to insert before/after.',
    )
    page_title = fields.Char('Page Title', default='Custom')

    @api.depends('model_id')
    def _compute_model_name(self):
        for rec in self:
            rec.model_name = rec.model_id.model if rec.model_id else False

    def action_insert_field(self):
        self.ensure_one()
        field_name = self.field_id.name

        if self.position in ('after', 'before'):
            if not self.reference_field:
                raise UserError(_('Please specify the Reference Field.'))
            ref = self.reference_field.strip()
            if not SAFE_NAME_RE.match(ref):
                raise UserError(_(
                    'Invalid reference field name: "%s". Use only lowercase '
                    'letters, digits and underscores.', ref,
                ))

        data = etree.Element('data')
        xpath = etree.SubElement(data, 'xpath')

        if self.position == 'after':
            xpath.set('expr', "//field[@name='%s']" % self.reference_field.strip())
            xpath.set('position', 'after')
            fld = etree.SubElement(xpath, 'field')
            fld.set('name', field_name)

        elif self.position == 'before':
            xpath.set('expr', "//field[@name='%s']" % self.reference_field.strip())
            xpath.set('position', 'before')
            fld = etree.SubElement(xpath, 'field')
            fld.set('name', field_name)

        elif self.position == 'sheet_end':
            xpath.set('expr', '//sheet')
            xpath.set('position', 'inside')
            group = etree.SubElement(xpath, 'group')
            group.set('string', 'Custom Fields')
            fld = etree.SubElement(group, 'field')
            fld.set('name', field_name)

        elif self.position == 'notebook_page':
            xpath.set('expr', '//notebook')
            xpath.set('position', 'inside')
            page = etree.SubElement(xpath, 'page')
            page.set('string', self.page_title or 'Custom')
            group = etree.SubElement(page, 'group')
            fld = etree.SubElement(group, 'field')
            fld.set('name', field_name)

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)

        view = self.env['ir.ui.view'].sudo().create({
            'name': 'studio_lite.insert.%s.%s' % (
                self.model_id.model.replace('.', '_'), field_name),
            'model': self.model_id.model,
            'inherit_id': self.view_id.id,
            'arch_db': arch,
            'priority': 9999,
        })

        tech_data = {
            'type': 'field_insert',
            'model': self.model_id.model,
            'field_name': field_name,
            'view_type': self.view_type,
            'position': self.position,
            'reference_field': self.reference_field or '',
            'target_view_id': self.view_id.id,
            'target_view_xmlid': self.view_id.get_external_id().get(self.view_id.id) or '',
            'arch': arch,
        }

        self.env['studio.customization'].sudo().create({
            'name': _('Inserted "%(field)s" into %(model)s %(vtype)s view',
                      field=self.field_id.field_description,
                      model=self.model_id.name, vtype=self.view_type),
            'model_id': self.model_id.id,
            'customization_type': 'field_insert',
            'field_id': self.field_id.id,
            'view_id': view.id,
            'target_view_id': self.view_id.id,
            'technical_data': json.dumps(tech_data, indent=2),
        })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Field Inserted'),
                'message': _('"%s" added to the view.', self.field_id.field_description),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
