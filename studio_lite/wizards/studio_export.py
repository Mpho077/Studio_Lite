import base64
import io
import json
import re
import zipfile
from collections import defaultdict

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class StudioExport(models.TransientModel):
    _name = 'studio.export'
    _description = 'Studio Lite — Export as Module'

    module_name = fields.Char(
        'Module Technical Name', default='studio_customizations', required=True,
    )
    module_description = fields.Char(
        'Module Title', default='Studio Lite Customizations',
    )
    model_ids = fields.Many2many(
        'ir.model', string='Models',
        help='Leave empty to export all customized models.',
    )
    export_fields = fields.Boolean('Include Custom Fields', default=True)
    export_views = fields.Boolean('Include View Changes', default=True)

    state = fields.Selection([
        ('choose', 'Choose'),
        ('done', 'Done'),
    ], default='choose')
    export_file = fields.Binary('Download', readonly=True)
    export_filename = fields.Char(readonly=True)

    def action_export(self):
        self.ensure_one()

        # Validate module name
        if not re.match(r'^[a-z][a-z0-9_]*$', self.module_name.strip()):
            raise UserError(_(
                'Invalid module name. Use only lowercase letters, digits '
                'and underscores, starting with a letter.',
            ))

        domain = [('state', '=', 'active')]
        if self.model_ids:
            domain.append(('model_id', 'in', self.model_ids.ids))

        customizations = self.env['studio.customization'].search(domain)
        if not customizations:
            raise UserError(_('No active customizations found.'))

        field_custs = customizations.filtered(
            lambda c: c.customization_type == 'field_create'
        ) if self.export_fields else self.env['studio.customization']
        view_custs = customizations.filtered(
            lambda c: c.view_id and c.view_id.exists()
        ) if self.export_views else self.env['studio.customization']

        if not field_custs and not view_custs:
            raise UserError(_('Nothing to export with the selected options.'))

        module = self.module_name.strip()
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Compute depends
            depends = self._compute_depends(customizations)
            data_files = []

            has_fields = bool(field_custs)
            has_views = bool(view_custs)

            if has_views:
                data_files.append('views/custom_views.xml')

            # __manifest__.py
            zf.writestr(
                '%s/__manifest__.py' % module,
                self._render_manifest(depends, data_files),
            )

            # __init__.py
            init_content = 'from . import models\n' if has_fields else ''
            zf.writestr('%s/__init__.py' % module, init_content)

            # models/
            if has_fields:
                zf.writestr('%s/models/__init__.py' % module,
                            'from . import custom_fields\n')
                zf.writestr('%s/models/custom_fields.py' % module,
                            self._generate_fields_py(field_custs))

            # views/
            if has_views:
                zf.writestr('%s/views/custom_views.xml' % module,
                            self._generate_views_xml(view_custs))

        zip_buffer.seek(0)
        self.write({
            'state': 'done',
            'export_file': base64.b64encode(zip_buffer.read()),
            'export_filename': '%s.zip' % module,
        })

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    # -------------------------------------------------------------------------
    # Generators
    # -------------------------------------------------------------------------

    def _compute_depends(self, customizations):
        depends = set()
        for cust in customizations:
            if cust.model_id.modules:
                first_mod = cust.model_id.modules.split(',')[0].strip()
                if first_mod:
                    depends.add(first_mod)
            if cust.target_view_id:
                xmlid = cust.target_view_id.get_external_id().get(
                    cust.target_view_id.id) or ''
                if xmlid and '.' in xmlid:
                    depends.add(xmlid.split('.')[0])
        depends.discard('studio_lite')
        depends.discard('')
        if 'base' not in depends:
            depends.add('base')
        return sorted(depends)

    def _render_manifest(self, depends, data_files):
        lines = [
            "{",
            "    'name': %r," % (self.module_description or self.module_name),
            "    'version': '19.0.1.0.0',",
            "    'category': 'Customizations',",
            "    'summary': 'Generated by Studio Lite',",
            "    'depends': %r," % depends,
            "    'data': [",
        ]
        for df in data_files:
            lines.append("        %r," % df)
        lines += [
            "    ],",
            "    'installable': True,",
            "    'auto_install': False,",
            "    'license': 'LGPL-3',",
            "}",
            "",
        ]
        return '\n'.join(lines)

    def _generate_fields_py(self, field_custs):
        lines = ['from odoo import models, fields', '', '']

        by_model = defaultdict(list)
        for cust in field_custs:
            if not cust.technical_data:
                continue
            data = json.loads(cust.technical_data)
            by_model[data['model']].append(data)

        for model, field_datas in sorted(by_model.items()):
            class_name = ''.join(
                part.capitalize() for part in model.replace('.', '_').split('_')
            ) + 'Custom'
            lines.append('')
            lines.append('class %s(models.Model):' % class_name)
            lines.append("    _inherit = '%s'" % model)
            lines.append('')

            for fd in field_datas:
                ft = fd['field_type']
                fname = fd['field_name']
                field_cls = {
                    'char': 'Char', 'text': 'Text', 'html': 'Html',
                    'integer': 'Integer', 'float': 'Float',
                    'monetary': 'Monetary', 'boolean': 'Boolean',
                    'date': 'Date', 'datetime': 'Datetime',
                    'binary': 'Binary', 'selection': 'Selection',
                    'many2one': 'Many2one', 'many2many': 'Many2many',
                }.get(ft, 'Char')

                params = []
                if ft in ('many2one', 'many2many') and fd.get('relation'):
                    params.append("'%s'" % fd['relation'])
                if ft == 'selection' and fd.get('selection'):
                    params.append(repr(fd['selection']))
                params.append("string='%s'" % fd['field_description'].replace("'", "\\'"))
                if fd.get('required'):
                    params.append('required=True')
                if fd.get('index'):
                    params.append('index=True')
                if fd.get('help'):
                    params.append("help='%s'" % fd['help'].replace("'", "\\'"))

                lines.append('    %s = fields.%s(%s)' % (
                    fname, field_cls, ', '.join(params)))

            lines.append('')

        return '\n'.join(lines)

    def _generate_views_xml(self, view_custs):
        lines = ['<?xml version="1.0" encoding="utf-8"?>', '<odoo>', '']

        for idx, cust in enumerate(view_custs, 1):
            if not cust.target_view_id:
                continue
            target = cust.target_view_id
            xmlid = target.get_external_id().get(target.id) or ''
            if not xmlid:
                continue  # Skip views without a stable external ID
            view = cust.view_id
            if not view or not view.exists():
                continue

            record_id = 'studio_custom_%d' % idx
            lines.append('    <record id="%s" model="ir.ui.view">' % record_id)
            lines.append('        <field name="name">%s</field>' % view.name)
            lines.append('        <field name="model">%s</field>' % view.model)
            lines.append('        <field name="inherit_id" ref="%s"/>' % xmlid)
            lines.append('        <field name="priority">9999</field>')
            lines.append('        <field name="arch" type="xml">')
            for arch_line in (view.arch_db or '').strip().split('\n'):
                lines.append('            %s' % arch_line)
            lines.append('        </field>')
            lines.append('    </record>')
            lines.append('')

        lines.append('</odoo>')
        lines.append('')
        return '\n'.join(lines)
