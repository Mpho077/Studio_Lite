import re
import json

from lxml import etree

from odoo import models, fields, api, _
from odoo.exceptions import UserError

SAFE_NAME_RE = re.compile(r'^[a-z_][a-z0-9_]*$')


class StudioFieldProperties(models.TransientModel):
    _name = 'studio.field.properties'
    _description = 'Studio Lite — Field Properties'

    model_id = fields.Many2one(
        'ir.model', 'Model', required=True,
        domain=[('transient', '=', False)],
    )
    model_name = fields.Char(compute='_compute_model_name')
    view_id = fields.Many2one(
        'ir.ui.view', 'View', required=True,
        domain="[('model', '=', model_name), ('type', 'in', ('form', 'list')), "
               "('inherit_id', '=', False)]",
    )
    target_field = fields.Char(
        'Field (Technical Name)', required=True,
        help='e.g. "partner_id", "date_order", "x_custom_field"',
    )

    # -- Properties to change -------------------------------------------------
    change_string = fields.Boolean('Change Label')
    new_string = fields.Char('New Label')

    change_required = fields.Boolean('Change Required')
    new_required = fields.Boolean('Required')

    change_readonly = fields.Boolean('Change Readonly')
    new_readonly = fields.Boolean('Readonly')

    change_widget = fields.Boolean('Change Widget')
    new_widget = fields.Char(
        'Widget',
        help='Widget technical name (e.g. "radio", "image", "monetary", '
             '"many2one_avatar", "statusbar", "priority").',
    )

    change_placeholder = fields.Boolean('Change Placeholder')
    new_placeholder = fields.Char('Placeholder')

    @api.depends('model_id')
    def _compute_model_name(self):
        for rec in self:
            rec.model_name = rec.model_id.model if rec.model_id else False

    def action_apply_properties(self):
        self.ensure_one()
        target = self.target_field.strip()
        if not SAFE_NAME_RE.match(target):
            raise UserError(_(
                'Invalid field name: "%s". Use only lowercase letters, '
                'digits and underscores.', target,
            ))

        data = etree.Element('data')
        xpath = etree.SubElement(data, 'xpath')
        xpath.set('expr', "//field[@name='%s']" % target)
        xpath.set('position', 'attributes')

        changes = []

        if self.change_string and self.new_string:
            attr = etree.SubElement(xpath, 'attribute')
            attr.set('name', 'string')
            attr.text = self.new_string
            changes.append('label="%s"' % self.new_string)

        if self.change_required:
            attr = etree.SubElement(xpath, 'attribute')
            attr.set('name', 'required')
            attr.text = '1' if self.new_required else '0'
            changes.append('required=%s' % self.new_required)

        if self.change_readonly:
            attr = etree.SubElement(xpath, 'attribute')
            attr.set('name', 'readonly')
            attr.text = '1' if self.new_readonly else '0'
            changes.append('readonly=%s' % self.new_readonly)

        if self.change_widget and self.new_widget:
            widget = self.new_widget.strip()
            if not SAFE_NAME_RE.match(widget):
                raise UserError(_(
                    'Invalid widget name: "%s". Use only lowercase letters, '
                    'digits and underscores.', widget,
                ))
            attr = etree.SubElement(xpath, 'attribute')
            attr.set('name', 'widget')
            attr.text = widget
            changes.append('widget="%s"' % widget)

        if self.change_placeholder and self.new_placeholder:
            attr = etree.SubElement(xpath, 'attribute')
            attr.set('name', 'placeholder')
            attr.text = self.new_placeholder
            changes.append('placeholder="%s"' % self.new_placeholder)

        if not changes:
            raise UserError(_('Please select at least one property to change.'))

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)

        view = self.env['ir.ui.view'].sudo().create({
            'name': 'studio_lite.props.%s.%s' % (
                self.model_id.model.replace('.', '_'), target),
            'model': self.model_id.model,
            'inherit_id': self.view_id.id,
            'arch_db': arch,
            'priority': 9999,
        })

        tech_data = {
            'type': 'field_property',
            'model': self.model_id.model,
            'target_field': target,
            'changes': changes,
            'target_view_id': self.view_id.id,
            'target_view_xmlid': self.view_id.get_external_id().get(self.view_id.id) or '',
            'arch': arch,
        }

        self.env['studio.customization'].sudo().create({
            'name': _('Changed "%(field)s" on %(model)s: %(changes)s',
                      field=target, model=self.model_id.name,
                      changes=', '.join(changes)),
            'model_id': self.model_id.id,
            'customization_type': 'field_property',
            'view_id': view.id,
            'target_view_id': self.view_id.id,
            'technical_data': json.dumps(tech_data, indent=2),
        })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Properties Updated'),
                'message': _('Updated: %s', ', '.join(changes)),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
