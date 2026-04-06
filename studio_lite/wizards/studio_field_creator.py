import re
import json

from lxml import etree

from odoo import models, fields, api, _
from odoo.exceptions import UserError

FIELD_NAME_RE = re.compile(r'^[a-z][a-z0-9_]*$')
SAFE_NAME_RE = re.compile(r'^[a-z_][a-z0-9_]*$')


class StudioFieldCreator(models.TransientModel):
    _name = 'studio.field.creator'
    _description = 'Studio Lite — Create Custom Field'

    # -- Model selection ------------------------------------------------------
    model_id = fields.Many2one(
        'ir.model', 'Model', required=True,
        domain=[('transient', '=', False)],
    )
    model_name = fields.Char(compute='_compute_model_name')

    # -- Field definition -----------------------------------------------------
    field_name = fields.Char(
        'Field Name', required=True,
        help='Technical name without x_ prefix (e.g. "custom_rating").',
    )
    field_description = fields.Char('Label', required=True)
    field_type = fields.Selection([
        ('char', 'Short Text'),
        ('text', 'Long Text'),
        ('html', 'Rich Text (HTML)'),
        ('integer', 'Whole Number'),
        ('float', 'Decimal Number'),
        ('monetary', 'Monetary'),
        ('boolean', 'Checkbox'),
        ('date', 'Date'),
        ('datetime', 'Date & Time'),
        ('binary', 'File / Attachment'),
        ('selection', 'Dropdown (Selection)'),
        ('many2one', 'Many2one (Link)'),
        ('many2many', 'Many2many (Tags)'),
    ], 'Field Type', required=True, default='char')

    # -- Selection options ----------------------------------------------------
    selection_options = fields.Text(
        'Dropdown Options',
        help='One per line:  value,Label\n\nExample:\ndraft,Draft\nconfirmed,Confirmed\ndone,Done',
    )

    # -- Relational -----------------------------------------------------------
    relation_model_id = fields.Many2one(
        'ir.model', 'Related Model',
        help='Target model for Many2one / Many2many fields.',
    )

    # -- Properties -----------------------------------------------------------
    is_required = fields.Boolean('Required')
    is_index = fields.Boolean('Indexed', help='Add a database index for faster searching.')
    help_text = fields.Text('Tooltip')

    # -- View insertion -------------------------------------------------------
    add_to_form = fields.Boolean('Add to Form View')
    target_view_id = fields.Many2one(
        'ir.ui.view', 'Target Form View',
        domain="[('model', '=', model_name), ('type', '=', 'form'), ('inherit_id', '=', False)]",
    )
    insert_after_field = fields.Char(
        'Insert After Field',
        help='Technical name of the field to insert after (e.g. "partner_id"). '
             'Leave empty to add in a new group at the bottom of the form.',
    )

    # -- Computed -------------------------------------------------------------
    @api.depends('model_id')
    def _compute_model_name(self):
        for rec in self:
            rec.model_name = rec.model_id.model if rec.model_id else False

    # -- Helpers --------------------------------------------------------------

    def _sanitize_field_name(self):
        name = self.field_name.strip().lower().replace(' ', '_')
        if not name.startswith('x_'):
            name = 'x_' + name
        return name

    def _validate_inputs(self, field_name):
        raw = field_name[2:] if field_name.startswith('x_') else field_name
        if not FIELD_NAME_RE.match(raw):
            raise UserError(_(
                'Invalid field name "%s". Use only lowercase letters, digits '
                'and underscores, starting with a letter.', field_name,
            ))
        existing = self.env['ir.model.fields'].search([
            ('model_id', '=', self.model_id.id),
            ('name', '=', field_name),
        ], limit=1)
        if existing:
            raise UserError(_(
                'Field "%s" already exists on model "%s".',
                field_name, self.model_id.model,
            ))
        if self.field_type == 'selection' and not self.selection_options:
            raise UserError(_('Please provide dropdown options for the Selection field.'))
        if self.field_type in ('many2one', 'many2many') and not self.relation_model_id:
            raise UserError(_('Please select a Related Model for this relational field.'))
        if self.add_to_form and self.insert_after_field:
            ref = self.insert_after_field.strip()
            if not SAFE_NAME_RE.match(ref):
                raise UserError(_(
                    'Invalid reference field name: "%s". Use only lowercase '
                    'letters, digits and underscores.', ref,
                ))

    def _parse_selection_options(self):
        if not self.selection_options:
            return []
        result = []
        for line in self.selection_options.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            if ',' not in line:
                raise UserError(_(
                    'Invalid selection option: "%s". Expected format: value,Label', line,
                ))
            value, label = line.split(',', 1)
            value, label = value.strip(), label.strip()
            if not value or not label:
                raise UserError(_(
                    'Both value and label are required. Got: "%s"', line,
                ))
            result.append((0, 0, {'value': value, 'name': label}))
        if not result:
            raise UserError(_('At least one selection option is required.'))
        return result

    # -- Main action ----------------------------------------------------------

    def action_create_field(self):
        self.ensure_one()
        field_name = self._sanitize_field_name()
        self._validate_inputs(field_name)

        vals = {
            'model_id': self.model_id.id,
            'name': field_name,
            'field_description': self.field_description,
            'ttype': self.field_type,
            'required': self.is_required,
            'index': self.is_index,
            'help': self.help_text or False,
            'store': True,
        }
        if self.field_type == 'selection':
            vals['selection_ids'] = self._parse_selection_options()
        if self.field_type in ('many2one', 'many2many'):
            vals['relation'] = self.relation_model_id.model
            if self.field_type == 'many2one':
                vals['on_delete'] = 'set null'

        new_field = self.env['ir.model.fields'].sudo().create(vals)

        # Build technical data for export
        tech_data = {
            'type': 'field_create',
            'model': self.model_id.model,
            'field_name': field_name,
            'field_type': self.field_type,
            'field_description': self.field_description,
            'required': self.is_required,
            'index': self.is_index,
            'help': self.help_text or '',
        }
        if self.field_type == 'selection':
            tech_data['selection'] = [
                (l.split(',', 1)[0].strip(), l.split(',', 1)[1].strip())
                for l in self.selection_options.strip().split('\n')
                if l.strip() and ',' in l
            ]
        if self.field_type in ('many2one', 'many2many'):
            tech_data['relation'] = self.relation_model_id.model

        cust_vals = {
            'name': _('Created field "%(label)s" (%(fname)s) on %(model)s',
                      label=self.field_description, fname=field_name,
                      model=self.model_id.name),
            'model_id': self.model_id.id,
            'customization_type': 'field_create',
            'field_id': new_field.id,
            'technical_data': json.dumps(tech_data, indent=2),
        }

        # Optionally insert into form view
        if self.add_to_form and self.target_view_id:
            view = self._create_view_inherit(field_name)
            cust_vals['view_id'] = view.id
            cust_vals['target_view_id'] = self.target_view_id.id
            tech_data['view_inherit'] = {
                'target_view_xmlid': self.target_view_id.get_external_id().get(self.target_view_id.id) or '',
                'arch': view.arch_db,
            }
            cust_vals['technical_data'] = json.dumps(tech_data, indent=2)

        self.env['studio.customization'].sudo().create(cust_vals)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Field Created'),
                'message': _('Field "%(label)s" created successfully on %(model)s.',
                            label=self.field_description, model=self.model_id.name),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    def _create_view_inherit(self, field_name):
        """Create an inherited view that adds the new field."""
        data = etree.Element('data')
        xpath = etree.SubElement(data, 'xpath')

        if self.insert_after_field:
            ref = self.insert_after_field.strip()
            xpath.set('expr', "//field[@name='%s']" % ref)
            xpath.set('position', 'after')
            fld = etree.SubElement(xpath, 'field')
            fld.set('name', field_name)
        else:
            xpath.set('expr', '//sheet')
            xpath.set('position', 'inside')
            group = etree.SubElement(xpath, 'group')
            group.set('string', 'Custom Fields')
            fld = etree.SubElement(group, 'field')
            fld.set('name', field_name)

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)
        return self.env['ir.ui.view'].sudo().create({
            'name': 'studio_lite.%s.%s' % (
                self.model_id.model.replace('.', '_'), field_name),
            'model': self.model_id.model,
            'inherit_id': self.target_view_id.id,
            'arch_db': arch,
            'priority': 9999,
        })
