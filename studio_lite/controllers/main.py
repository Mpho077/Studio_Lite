import json
import re

from lxml import etree

from odoo import http, _
from odoo.http import request
from odoo.exceptions import UserError, AccessError

SAFE_NAME_RE = re.compile(r'^[a-z_][a-z0-9_.]*$')
FIELD_NAME_RE = re.compile(r'^[a-z][a-z0-9_]*$')


class StudioLiteController(http.Controller):
    """JSON-RPC endpoints consumed by the Studio Lite design panel."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_access():
        if not request.env.user.has_group('studio_lite.group_studio_user'):
            raise AccessError(_('Only Studio Lite users can perform this action.'))

    @staticmethod
    def _validate_name(name, label='name'):
        if not SAFE_NAME_RE.match(name):
            raise UserError(_(
                'Invalid %(label)s: "%(name)s". Use only lowercase letters, '
                'digits, underscores and dots.',
                label=label, name=name,
            ))

    @staticmethod
    def _get_primary_view(model, view_type):
        """Return the primary (non-inherited) view for the given model/type."""
        View = request.env['ir.ui.view'].sudo()
        view = View.search([
            ('model', '=', model),
            ('type', '=', view_type),
            ('inherit_id', '=', False),
        ], order='priority, id', limit=1)
        return view

    def _parse_view_layout(self, combined_arch, fields_get):
        """Parse form view arch into a hierarchical layout dict for the design panel."""
        try:
            tree = etree.fromstring(combined_arch)
        except Exception:
            return {'type': 'sheet', 'groups': [], 'notebook': None}

        def fi(fname, node=None):
            f = fields_get.get(fname, {})
            va = {}
            if node is not None:
                for a in ('string', 'required', 'readonly', 'invisible',
                          'placeholder', 'help', 'colspan'):
                    v = node.get(a)
                    if v is not None:
                        va[a] = v
            return {'name': fname, 'string': f.get('string', fname),
                    'type': f.get('type', 'char'), 'view_attrs': va}

        def parse_group_node(node, xpath):
            label = node.get('string', '')
            subgroups = [c for c in node if c.tag == 'group']
            result = {'type': 'group', 'label': label, 'xpath': xpath,
                      'columns': [], 'fields': []}
            if subgroups:
                for i, sg in enumerate(subgroups, 1):
                    sg_xpath = xpath + '/group[%d]' % i
                    result['columns'].append({
                        'type': 'column',
                        'label': sg.get('string', ''),
                        'xpath': sg_xpath,
                        'fields': [fi(c.get('name'), c) for c in sg
                                   if c.tag == 'field' and c.get('name')],
                    })
            else:
                result['fields'] = [fi(c.get('name'), c) for c in node
                                    if c.tag == 'field' and c.get('name')]
            return result

        def parse_page_node(node, index):
            label = node.get('string') or node.get('name') or ('Page %d' % index)
            invisible = node.get('invisible', '')
            # Never use @string as xpath selector — it's translatable and Odoo rejects it.
            # Prefer @name, then fall back to positional index.
            if node.get('name'):
                xpath = "//page[@name='%s']" % node.get('name')
            else:
                xpath = '//notebook/page[%d]' % index
            groups = []
            group_idx = 0
            for child in node:
                if child.tag == 'group':
                    group_idx += 1
                    groups.append(parse_group_node(
                        child, xpath + '/group[%d]' % group_idx))
            direct_fields = [fi(c.get('name'), c) for c in node
                             if c.tag == 'field' and c.get('name')]
            return {
                'type': 'page', 'label': label, 'xpath': xpath,
                'invisible': invisible not in ('', '0', 'False', 'false'),
                'groups': groups, 'fields': direct_fields,
            }

        sheet = tree.find('.//sheet')
        root = sheet if sheet is not None else tree
        layout = {'type': 'sheet', 'groups': [], 'notebook': None}
        group_idx = 0
        for child in root:
            if child.tag == 'group':
                group_idx += 1
                layout['groups'].append(
                    parse_group_node(child, '//sheet/group[%d]' % group_idx))
            elif child.tag == 'notebook':
                pages = []
                page_idx = 0
                for pc in child:
                    if pc.tag == 'page':
                        page_idx += 1
                        pages.append(parse_page_node(pc, page_idx))
                layout['notebook'] = {
                    'type': 'notebook', 'xpath': '//notebook', 'pages': pages}
        return layout

    def _ensure_studio_group(self, model, target_view):
        """
        Find or create a single shared 'Custom Fields' group for this model/view.
        Also migrates any old duplicate-group insert views to target this single group.
        Returns the xpath to that group so fields can be inserted into it.
        """
        View = request.env['ir.ui.view'].sudo()
        group_name = 'studio_lite.studio_group.%s' % model.replace('.', '_')
        existing = View.search([
            ('name', '=', group_name),
            ('model', '=', model),
            ('inherit_id', '=', target_view.id),
        ], limit=1)
        if not existing:
            # Determine insertion point: before notebook or at end of sheet
            combined_arch = target_view.get_combined_arch()
            tree = etree.fromstring(combined_arch)
            sheet = tree.find('.//sheet') or tree
            has_notebook = sheet.find('notebook') is not None

            data = etree.Element('data')
            xpath_el = etree.SubElement(data, 'xpath')
            if has_notebook:
                xpath_el.set('expr', '//notebook')
                xpath_el.set('position', 'before')
            else:
                xpath_el.set('expr', '//sheet')
                xpath_el.set('position', 'inside')
            group = etree.SubElement(xpath_el, 'group')
            group.set('string', 'Custom Fields')
            group.set('name', 'studio_custom')

            arch = etree.tostring(data, encoding='unicode', pretty_print=True)
            View.create({
                'name': group_name,
                'model': model,
                'inherit_id': target_view.id,
                'arch_db': arch,
                'priority': 9998,
            })

        # Migrate any old insert views that still wrap fields in a bare <group> element
        self._migrate_old_custom_groups(model, target_view)
        return '//group[@name="studio_custom"]'

    @staticmethod
    def _migrate_old_custom_groups(model, target_view):
        """Convert old-style insert views that embed <group string="Custom Fields"><field/></group>
        into direct insertions targeting //group[@name='studio_custom']."""
        import copy
        View = request.env['ir.ui.view'].sudo()
        old_inserts = View.search([
            ('model', '=', model),
            ('inherit_id', '=', target_view.id),
            ('name', 'like', 'studio_lite.insert.%'),
            ('arch_db', 'ilike', 'Custom Fields'),
        ])
        for v in old_inserts:
            try:
                tree = etree.fromstring(v.arch_db)
                new_data = etree.Element('data')
                migrated = False
                for xpath_el in tree.findall('xpath'):
                    for child in list(xpath_el):
                        if child.tag == 'group' and 'Custom Fields' in child.get('string', ''):
                            for field_el in list(child):
                                if field_el.tag == 'field':
                                    nx = etree.SubElement(new_data, 'xpath')
                                    nx.set('expr', '//group[@name="studio_custom"]')
                                    nx.set('position', 'inside')
                                    nx.append(copy.deepcopy(field_el))
                                    migrated = True
                if migrated:
                    new_arch = etree.tostring(new_data, encoding='unicode', pretty_print=True)
                    v.write({'arch_db': new_arch})
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Get all fields + view fields
    # ------------------------------------------------------------------

    @http.route('/studio_lite/get_view_fields', type='json', auth='user')
    def get_view_fields(self, model, view_type='form'):
        self._check_access()
        self._validate_name(model, 'model')

        Model = request.env[model].sudo()
        view = self._get_primary_view(model, view_type)
        if not view:
            return {
                'all_fields': [],
                'view_fields': [],
                'hidden_fields': [],
                'model_id': None,
                'view_id': None,
                'view_name': '',
            }

        # All fields on the model
        fields_get = Model.fields_get()
        all_fields = []
        for fname, finfo in sorted(fields_get.items()):
            all_fields.append({
                'name': fname,
                'string': finfo.get('string', fname),
                'type': finfo.get('type', 'char'),
                'required': finfo.get('required', False),
                'readonly': finfo.get('readonly', False),
                'store': finfo.get('store', True),
            })

        # Fields in the combined (arch + inherited) view
        combined_arch = view.with_context(studio_lite=True).get_combined_arch()
        tree = etree.fromstring(combined_arch)
        view_field_names = []
        for fnode in tree.iter('field'):
            fname = fnode.get('name')
            if fname and fname not in view_field_names:
                view_field_names.append(fname)

        view_fields = []
        for fname in view_field_names:
            finfo = fields_get.get(fname, {})
            view_fields.append({
                'name': fname,
                'string': finfo.get('string', fname),
                'type': finfo.get('type', 'char'),
                'required': finfo.get('required', False),
                'readonly': finfo.get('readonly', False),
            })

        # Fields hidden by Studio Lite
        hidden_fields = []
        studio_views = request.env['ir.ui.view'].sudo().search([
            ('model', '=', model),
            ('inherit_id', '=', view.id),
            ('name', 'like', 'studio_lite.hide.'),
        ])
        for sv in studio_views:
            try:
                sv_tree = etree.fromstring(sv.arch_db)
                for xpath in sv_tree.iter('xpath'):
                    expr = xpath.get('expr', '')
                    if 'invisible' in etree.tostring(xpath, encoding='unicode'):
                        # Extract field name from expr like //field[@name='xxx']
                        match = re.search(r"@name='([^']+)'", expr)
                        if match:
                            hidden_fields.append(match.group(1))
            except Exception:
                pass

        ir_model = request.env['ir.model'].sudo().search(
            [('model', '=', model)], limit=1)

        layout = self._parse_view_layout(combined_arch, fields_get)

        return {
            'all_fields': all_fields,
            'view_fields': view_fields,
            'hidden_fields': hidden_fields,
            'model_id': ir_model.id if ir_model else None,
            'view_id': view.id,
            'view_name': view.name,
            'layout': layout,
        }

    # ------------------------------------------------------------------
    # Toggle field visibility
    # ------------------------------------------------------------------

    @http.route('/studio_lite/toggle_field_visibility', type='json', auth='user')
    def toggle_field_visibility(self, model, view_type, view_id,
                                 field_name, hide=True):
        self._check_access()
        self._validate_name(model, 'model')
        self._validate_name(field_name, 'field_name')

        View = request.env['ir.ui.view'].sudo()
        target_view = View.browse(int(view_id))
        if not target_view.exists():
            raise UserError(_('Target view not found.'))

        if hide:
            # Create an inherited view that hides the field
            data = etree.Element('data')
            xpath = etree.SubElement(data, 'xpath')
            xpath.set('expr', "//field[@name='%s']" % field_name)
            xpath.set('position', 'attributes')

            if view_type == 'list':
                attr = etree.SubElement(xpath, 'attribute')
                attr.set('name', 'column_invisible')
                attr.text = 'True'
            else:
                attr = etree.SubElement(xpath, 'attribute')
                attr.set('name', 'invisible')
                attr.text = '1'

            arch = etree.tostring(data, encoding='unicode', pretty_print=True)
            view = View.create({
                'name': 'studio_lite.hide.%s.%s' % (
                    model.replace('.', '_'), field_name),
                'model': model,
                'inherit_id': target_view.id,
                'arch_db': arch,
                'priority': 9999,
            })

            ir_model = request.env['ir.model'].sudo().search(
                [('model', '=', model)], limit=1)
            request.env['studio.customization'].sudo().create({
                'name': _('Hidden "%(field)s" on %(model)s %(vtype)s view (design panel)',
                          field=field_name, model=model, vtype=view_type),
                'model_id': ir_model.id,
                'customization_type': 'field_hide',
                'view_id': view.id,
                'target_view_id': target_view.id,
                'technical_data': json.dumps({
                    'type': 'field_hide',
                    'model': model,
                    'field_name': field_name,
                    'view_type': view_type,
                    'arch': arch,
                }),
            })
        else:
            # Find and remove the hide view
            hide_views = View.search([
                ('model', '=', model),
                ('inherit_id', '=', target_view.id),
                ('name', 'like', 'studio_lite.hide.%s.%s' % (
                    model.replace('.', '_'), field_name)),
            ])
            for hv in hide_views:
                # Also remove customization log entry
                custs = request.env['studio.customization'].sudo().search([
                    ('view_id', '=', hv.id),
                    ('state', '=', 'active'),
                ])
                custs.write({'state': 'reverted'})
                hv.unlink()

        return {'success': True}

    # ------------------------------------------------------------------
    # Add existing field to view
    # ------------------------------------------------------------------

    @http.route('/studio_lite/add_field_to_view', type='json', auth='user')
    def add_field_to_view(self, model, view_type, view_id, field_name):
        self._check_access()
        self._validate_name(model, 'model')
        self._validate_name(field_name, 'field_name')

        View = request.env['ir.ui.view'].sudo()
        target_view = View.browse(int(view_id))
        if not target_view.exists():
            raise UserError(_('Target view not found.'))

        data = etree.Element('data')
        xpath = etree.SubElement(data, 'xpath')

        if view_type == 'list':
            xpath.set('expr', '//list')
            xpath.set('position', 'inside')
            fld = etree.SubElement(xpath, 'field')
            fld.set('name', field_name)
        else:
            # Smart insert: reuse single 'Custom Fields' group (no duplicates)
            group_xpath = self._ensure_studio_group(model, target_view)
            xpath.set('expr', group_xpath)
            xpath.set('position', 'inside')
            fld = etree.SubElement(xpath, 'field')
            fld.set('name', field_name)

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)
        view = View.create({
            'name': 'studio_lite.insert.%s.%s' % (
                model.replace('.', '_'), field_name),
            'model': model,
            'inherit_id': target_view.id,
            'arch_db': arch,
            'priority': 9999,
        })

        ir_model = request.env['ir.model'].sudo().search(
            [('model', '=', model)], limit=1)
        field = request.env['ir.model.fields'].sudo().search([
            ('model_id', '=', ir_model.id),
            ('name', '=', field_name),
        ], limit=1)

        request.env['studio.customization'].sudo().create({
            'name': _('Inserted "%(field)s" into %(model)s %(vtype)s (design panel)',
                      field=field_name, model=model, vtype=view_type),
            'model_id': ir_model.id,
            'customization_type': 'field_insert',
            'field_id': field.id if field else False,
            'view_id': view.id,
            'target_view_id': target_view.id,
            'technical_data': json.dumps({
                'type': 'field_insert',
                'model': model,
                'field_name': field_name,
                'view_type': view_type,
                'arch': arch,
            }),
        })

        return {'success': True}

    # ------------------------------------------------------------------
    # Create field + add to view
    # ------------------------------------------------------------------

    @http.route('/studio_lite/create_field', type='json', auth='user')
    def create_field(self, model, view_type, view_id, field_name, field_label,
                     field_type='char', selection_options='', relation_model=''):
        self._check_access()
        self._validate_name(model, 'model')

        # Clean up field name
        name = field_name.strip().lower().replace(' ', '_')
        if not name.startswith('x_'):
            name = 'x_' + name

        raw = name[2:] if name.startswith('x_') else name
        if not FIELD_NAME_RE.match(raw):
            raise UserError(_(
                'Invalid field name "%s". Use only lowercase letters, '
                'digits and underscores.', name))

        ir_model = request.env['ir.model'].sudo().search(
            [('model', '=', model)], limit=1)
        if not ir_model:
            raise UserError(_('Model "%s" not found.', model))

        # Check for duplicates
        existing = request.env['ir.model.fields'].sudo().search([
            ('model_id', '=', ir_model.id),
            ('name', '=', name),
        ], limit=1)
        if existing:
            raise UserError(_(
                'Field "%s" already exists on model "%s".', name, model))

        vals = {
            'model_id': ir_model.id,
            'name': name,
            'field_description': field_label,
            'ttype': field_type,
            'store': True,
        }

        if field_type == 'selection' and selection_options:
            sel_ids = []
            for line in selection_options.strip().split('\n'):
                line = line.strip()
                if not line or ',' not in line:
                    continue
                val, lbl = line.split(',', 1)
                sel_ids.append((0, 0, {'value': val.strip(), 'name': lbl.strip()}))
            if sel_ids:
                vals['selection_ids'] = sel_ids

        if field_type in ('many2one', 'many2many') and relation_model:
            self._validate_name(relation_model, 'relation model')
            vals['relation'] = relation_model
            if field_type == 'many2one':
                vals['on_delete'] = 'set null'

        new_field = request.env['ir.model.fields'].sudo().create(vals)

        # Now add field to view
        self.add_field_to_view(model, view_type, view_id, name)

        # Log under field_create type
        cust = request.env['studio.customization'].sudo().search([
            ('model_id', '=', ir_model.id),
            ('customization_type', '=', 'field_insert'),
            ('field_id', '=', False),
        ], order='id desc', limit=1)
        if cust:
            cust.write({
                'customization_type': 'field_create',
                'field_id': new_field.id,
                'name': _('Created "%(label)s" (%(name)s) on %(model)s (design panel)',
                          label=field_label, name=name, model=model),
            })

        return {'success': True}

    # ------------------------------------------------------------------
    # Move / reorder field
    # ------------------------------------------------------------------

    @http.route('/studio_lite/move_field', type='json', auth='user')
    def move_field(self, model, view_type, view_id, field_name, direction='up'):
        self._check_access()
        self._validate_name(model, 'model')
        self._validate_name(field_name, 'field_name')

        View = request.env['ir.ui.view'].sudo()
        target_view = View.browse(int(view_id))
        if not target_view.exists():
            raise UserError(_('Target view not found.'))

        # Parse combined arch to find the field order
        combined_arch = target_view.with_context(
            studio_lite=True).get_combined_arch()
        tree = etree.fromstring(combined_arch)
        field_nodes = list(tree.iter('field'))
        field_names = []
        for fn in field_nodes:
            n = fn.get('name')
            if n and n not in field_names:
                field_names.append(n)

        if field_name not in field_names:
            raise UserError(_('Field "%s" not found in view.', field_name))

        idx = field_names.index(field_name)
        if direction == 'up' and idx <= 0:
            return {'success': True}
        if direction == 'down' and idx >= len(field_names) - 1:
            return {'success': True}

        # Determine the neighbor field
        if direction == 'up':
            neighbor = field_names[idx - 1]
            position = 'before'
        else:
            neighbor = field_names[idx + 1]
            position = 'after'

        return self.reorder_field(
            model, view_type, view_id, field_name, neighbor, position)

    @http.route('/studio_lite/reorder_field', type='json', auth='user')
    def reorder_field(self, model, view_type, view_id, field_name,
                       target_field, position='after'):
        self._check_access()
        self._validate_name(model, 'model')
        self._validate_name(field_name, 'field name')
        self._validate_name(target_field, 'target field')

        View = request.env['ir.ui.view'].sudo()
        target_view = View.browse(int(view_id))
        if not target_view.exists():
            raise UserError(_('Target view not found.'))

        # Remove any existing reorder view for this field
        old_views = View.search([
            ('model', '=', model),
            ('inherit_id', '=', target_view.id),
            ('name', 'like', 'studio_lite.reorder.%s.%s' % (
                model.replace('.', '_'), field_name)),
        ])
        old_custs = request.env['studio.customization'].sudo().search([
            ('view_id', 'in', old_views.ids),
        ])
        old_custs.write({'state': 'reverted'})
        old_views.unlink()

        # Create a 2-step xpath: first remove the field, then re-insert it
        data = etree.Element('data')

        # Step 1: replace original field with nothing (effectively remove)
        xpath_remove = etree.SubElement(data, 'xpath')
        xpath_remove.set('expr', "//field[@name='%s']" % field_name)
        xpath_remove.set('position', 'replace')
        # Empty replace = remove

        # Step 2: add it back at the new position
        xpath_add = etree.SubElement(data, 'xpath')
        xpath_add.set('expr', "//field[@name='%s']" % target_field)
        xpath_add.set('position', position)
        fld = etree.SubElement(xpath_add, 'field')
        fld.set('name', field_name)

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)
        view = View.create({
            'name': 'studio_lite.reorder.%s.%s' % (
                model.replace('.', '_'), field_name),
            'model': model,
            'inherit_id': target_view.id,
            'arch_db': arch,
            'priority': 9999,
        })

        ir_model = request.env['ir.model'].sudo().search(
            [('model', '=', model)], limit=1)
        request.env['studio.customization'].sudo().create({
            'name': _('Moved "%(field)s" %(pos)s "%(target)s" on %(model)s',
                      field=field_name, pos=position, target=target_field,
                      model=model),
            'model_id': ir_model.id if ir_model else False,
            'customization_type': 'field_insert',
            'view_id': view.id,
            'target_view_id': target_view.id,
            'technical_data': json.dumps({
                'type': 'field_reorder',
                'model': model,
                'field_name': field_name,
                'target_field': target_field,
                'position': position,
                'arch': arch,
            }),
        })

        return {'success': True}

    # ------------------------------------------------------------------
    # Add field to a specific container (group / column / page)
    # ------------------------------------------------------------------

    @http.route('/studio_lite/add_field_to_container', type='json', auth='user')
    def add_field_to_container(self, model, view_type, view_id,
                                field_name, container_xpath):
        self._check_access()
        self._validate_name(model, 'model')
        self._validate_name(field_name, 'field_name')

        View = request.env['ir.ui.view'].sudo()
        target_view = View.browse(int(view_id))
        if not target_view.exists():
            raise UserError(_('Target view not found.'))

        data = etree.Element('data')
        xpath_el = etree.SubElement(data, 'xpath')
        xpath_el.set('expr', container_xpath)
        xpath_el.set('position', 'inside')
        fld = etree.SubElement(xpath_el, 'field')
        fld.set('name', field_name)

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)
        view = View.create({
            'name': 'studio_lite.insert.%s.%s' % (
                model.replace('.', '_'), field_name),
            'model': model,
            'inherit_id': target_view.id,
            'arch_db': arch,
            'priority': 9999,
        })

        ir_model = request.env['ir.model'].sudo().search(
            [('model', '=', model)], limit=1)
        field = request.env['ir.model.fields'].sudo().search([
            ('model_id', '=', ir_model.id if ir_model else 0),
            ('name', '=', field_name),
        ], limit=1)
        request.env['studio.customization'].sudo().create({
            'name': _('Inserted "%(field)s" into %(model)s at %(xpath)s',
                      field=field_name, model=model, xpath=container_xpath),
            'model_id': ir_model.id if ir_model else False,
            'customization_type': 'field_insert',
            'field_id': field.id if field else False,
            'view_id': view.id,
            'target_view_id': target_view.id,
            'technical_data': json.dumps({
                'type': 'field_insert_container',
                'model': model,
                'field_name': field_name,
                'container_xpath': container_xpath,
                'arch': arch,
            }),
        })
        return {'success': True}

    # ------------------------------------------------------------------
    # Add a new section (group) to the form
    # ------------------------------------------------------------------

    @http.route('/studio_lite/add_section', type='json', auth='user')
    def add_section(self, model, view_id, section_title):
        self._check_access()
        self._validate_name(model, 'model')

        title = (section_title or '').strip()
        if not title:
            raise UserError(_('Section title is required.'))

        View = request.env['ir.ui.view'].sudo()
        target_view = View.browse(int(view_id))
        if not target_view.exists():
            raise UserError(_('Target view not found.'))

        combined_arch = target_view.get_combined_arch()
        tree = etree.fromstring(combined_arch)
        sheet = tree.find('.//sheet') or tree
        has_notebook = sheet.find('notebook') is not None

        data = etree.Element('data')
        xpath_el = etree.SubElement(data, 'xpath')
        if has_notebook:
            xpath_el.set('expr', '//notebook')
            xpath_el.set('position', 'before')
        else:
            xpath_el.set('expr', '//sheet')
            xpath_el.set('position', 'inside')
        group = etree.SubElement(xpath_el, 'group')
        group.set('string', title)

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)
        safe = re.sub(r'[^a-z0-9]', '_', title.lower())[:20]
        view = View.create({
            'name': 'studio_lite.section.%s.%s' % (
                model.replace('.', '_'), safe),
            'model': model,
            'inherit_id': target_view.id,
            'arch_db': arch,
            'priority': 9999,
        })

        ir_model = request.env['ir.model'].sudo().search(
            [('model', '=', model)], limit=1)
        request.env['studio.customization'].sudo().create({
            'name': _('Added section "%s" on %s', title, model),
            'model_id': ir_model.id if ir_model else False,
            'customization_type': 'field_insert',
            'view_id': view.id,
            'target_view_id': target_view.id,
            'technical_data': json.dumps({
                'type': 'section_create',
                'model': model,
                'section_title': title,
                'arch': arch,
            }),
        })
        return {'success': True}

    # ------------------------------------------------------------------
    # Add a new tab (notebook page)
    # ------------------------------------------------------------------

    @http.route('/studio_lite/add_tab', type='json', auth='user')
    def add_tab(self, model, view_id, tab_title):
        self._check_access()
        self._validate_name(model, 'model')

        title = (tab_title or '').strip()
        if not title:
            raise UserError(_('Tab title is required.'))

        View = request.env['ir.ui.view'].sudo()
        target_view = View.browse(int(view_id))
        if not target_view.exists():
            raise UserError(_('Target view not found.'))

        combined_arch = target_view.get_combined_arch()
        tree = etree.fromstring(combined_arch)
        sheet = tree.find('.//sheet') or tree
        notebook = sheet.find('notebook')

        data = etree.Element('data')
        xpath_el = etree.SubElement(data, 'xpath')

        safe = re.sub(r'[^a-z0-9]', '_', title.lower())[:20]
        page_name = 'studio_tab_%s' % safe

        if notebook is not None:
            xpath_el.set('expr', '//notebook')
            xpath_el.set('position', 'inside')
            page = etree.SubElement(xpath_el, 'page')
            page.set('string', title)
            page.set('name', page_name)   # @name allows safe xpath targeting later
            etree.SubElement(page, 'group')
        else:
            # No notebook yet — create one inside sheet
            xpath_el.set('expr', '//sheet')
            xpath_el.set('position', 'inside')
            nb = etree.SubElement(xpath_el, 'notebook')
            page = etree.SubElement(nb, 'page')
            page.set('string', title)
            page.set('name', page_name)
            etree.SubElement(page, 'group')

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)
        view = View.create({
            'name': 'studio_lite.tab.%s.%s' % (
                model.replace('.', '_'), safe),
            'model': model,
            'inherit_id': target_view.id,
            'arch_db': arch,
            'priority': 9999,
        })

        ir_model = request.env['ir.model'].sudo().search(
            [('model', '=', model)], limit=1)
        request.env['studio.customization'].sudo().create({
            'name': _('Added tab "%s" on %s', title, model),
            'model_id': ir_model.id if ir_model else False,
            'customization_type': 'field_insert',
            'view_id': view.id,
            'target_view_id': target_view.id,
            'technical_data': json.dumps({
                'type': 'tab_create',
                'model': model,
                'tab_title': title,
                'arch': arch,
            }),
        })
        return {'success': True}

    # ------------------------------------------------------------------
    # Add a column (sub-group) to an existing group
    # ------------------------------------------------------------------

    @http.route('/studio_lite/add_column', type='json', auth='user')
    def add_column(self, model, view_id, group_xpath, column_title=''):
        self._check_access()
        self._validate_name(model, 'model')

        View = request.env['ir.ui.view'].sudo()
        target_view = View.browse(int(view_id))
        if not target_view.exists():
            raise UserError(_('Target view not found.'))

        data = etree.Element('data')
        xpath_el = etree.SubElement(data, 'xpath')
        xpath_el.set('expr', group_xpath)
        xpath_el.set('position', 'inside')
        new_col = etree.SubElement(xpath_el, 'group')
        if column_title:
            new_col.set('string', column_title.strip())

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)
        safe = re.sub(r'[^a-z0-9]', '_', group_xpath)[:30]
        view = View.create({
            'name': 'studio_lite.col.%s.%s' % (model.replace('.', '_'), safe),
            'model': model,
            'inherit_id': target_view.id,
            'arch_db': arch,
            'priority': 9999,
        })

        ir_model = request.env['ir.model'].sudo().search(
            [('model', '=', model)], limit=1)
        request.env['studio.customization'].sudo().create({
            'name': _('Added column to %s at %s', model, group_xpath),
            'model_id': ir_model.id if ir_model else False,
            'customization_type': 'field_insert',
            'view_id': view.id,
            'target_view_id': target_view.id,
            'technical_data': json.dumps({
                'type': 'column_add',
                'model': model,
                'group_xpath': group_xpath,
                'arch': arch,
            }),
        })
        return {'success': True}

    # ------------------------------------------------------------------
    # Delete a column (sub-group) from a group
    # ------------------------------------------------------------------

    @http.route('/studio_lite/delete_column', type='json', auth='user')
    def delete_column(self, model, view_id, column_xpath):
        self._check_access()
        self._validate_name(model, 'model')

        View = request.env['ir.ui.view'].sudo()
        target_view = View.browse(int(view_id))
        if not target_view.exists():
            raise UserError(_('Target view not found.'))

        data = etree.Element('data')
        xpath_el = etree.SubElement(data, 'xpath')
        xpath_el.set('expr', column_xpath)
        xpath_el.set('position', 'replace')
        # Empty replace removes the element

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)
        safe = re.sub(r'[^a-z0-9]', '_', column_xpath)[:30]
        view = View.create({
            'name': 'studio_lite.delcol.%s.%s' % (model.replace('.', '_'), safe),
            'model': model,
            'inherit_id': target_view.id,
            'arch_db': arch,
            'priority': 9999,
        })

        ir_model = request.env['ir.model'].sudo().search(
            [('model', '=', model)], limit=1)
        request.env['studio.customization'].sudo().create({
            'name': _('Deleted column %s from %s', column_xpath, model),
            'model_id': ir_model.id if ir_model else False,
            'customization_type': 'field_insert',
            'view_id': view.id,
            'target_view_id': target_view.id,
            'technical_data': json.dumps({
                'type': 'column_delete',
                'model': model,
                'column_xpath': column_xpath,
                'arch': arch,
            }),
        })
        return {'success': True}

    # ------------------------------------------------------------------
    # Rename a column (set/clear @string on a sub-group)
    # ------------------------------------------------------------------

    @http.route('/studio_lite/rename_column', type='json', auth='user')
    def rename_column(self, model, view_id, column_xpath, new_title=''):
        self._check_access()
        self._validate_name(model, 'model')

        View = request.env['ir.ui.view'].sudo()
        target_view = View.browse(int(view_id))
        if not target_view.exists():
            raise UserError(_('Target view not found.'))

        data = etree.Element('data')
        xpath_el = etree.SubElement(data, 'xpath')
        xpath_el.set('expr', column_xpath)
        xpath_el.set('position', 'attributes')
        attr = etree.SubElement(xpath_el, 'attribute')
        attr.set('name', 'string')
        attr.text = (new_title or '').strip()

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)
        safe = re.sub(r'[^a-z0-9]', '_', column_xpath)[:30]
        view = View.create({
            'name': 'studio_lite.renamecol.%s.%s' % (model.replace('.', '_'), safe),
            'model': model,
            'inherit_id': target_view.id,
            'arch_db': arch,
            'priority': 9999,
        })

        ir_model = request.env['ir.model'].sudo().search(
            [('model', '=', model)], limit=1)
        request.env['studio.customization'].sudo().create({
            'name': _('Renamed column at %s on %s', column_xpath, model),
            'model_id': ir_model.id if ir_model else False,
            'customization_type': 'field_insert',
            'view_id': view.id,
            'target_view_id': target_view.id,
            'technical_data': json.dumps({
                'type': 'column_rename',
                'model': model,
                'column_xpath': column_xpath,
                'new_title': new_title,
                'arch': arch,
            }),
        })
        return {'success': True}

    # ------------------------------------------------------------------
    # Remove a field from the view (position="replace" with empty content)
    # ------------------------------------------------------------------

    @http.route('/studio_lite/remove_field_from_view', type='json', auth='user')
    def remove_field_from_view(self, model, view_id, field_name):
        self._check_access()
        self._validate_name(model, 'model')
        self._validate_name(field_name, 'field_name')

        View = request.env['ir.ui.view'].sudo()
        target_view = View.browse(int(view_id))
        if not target_view.exists():
            raise UserError(_('Target view not found.'))

        data = etree.Element('data')
        xpath_el = etree.SubElement(data, 'xpath')
        xpath_el.set('expr', "//field[@name='%s']" % field_name)
        xpath_el.set('position', 'replace')
        # Empty replace = remove the element entirely

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)
        view = View.create({
            'name': 'studio_lite.remove.%s.%s' % (
                model.replace('.', '_'), field_name),
            'model': model,
            'inherit_id': target_view.id,
            'arch_db': arch,
            'priority': 9999,
        })

        ir_model = request.env['ir.model'].sudo().search(
            [('model', '=', model)], limit=1)
        field = request.env['ir.model.fields'].sudo().search([
            ('model_id', '=', ir_model.id if ir_model else 0),
            ('name', '=', field_name),
        ], limit=1)
        request.env['studio.customization'].sudo().create({
            'name': _('Removed "%(field)s" from %(model)s view',
                      field=field_name, model=model),
            'model_id': ir_model.id if ir_model else False,
            'customization_type': 'field_insert',
            'field_id': field.id if field else False,
            'view_id': view.id,
            'target_view_id': target_view.id,
            'technical_data': json.dumps({
                'type': 'field_remove',
                'model': model,
                'field_name': field_name,
                'arch': arch,
            }),
        })
        return {'success': True}

    # ------------------------------------------------------------------
    # Update field view-level attributes (label override, required, etc.)
    # ------------------------------------------------------------------

    @http.route('/studio_lite/update_field_attrs', type='json', auth='user')
    def update_field_attrs(self, model, view_id, field_name, attrs=None):
        """Set/update view-level attributes on a field node.
        Attrs supported: string, required, readonly, placeholder, help, colspan.
        Empty attrs dict = clean up any existing override view."""
        self._check_access()
        self._validate_name(model, 'model')
        self._validate_name(field_name, 'field_name')

        ALLOWED = {'string', 'required', 'readonly', 'placeholder', 'help', 'colspan'}
        safe_attrs = {k: v for k, v in (attrs or {}).items()
                      if k in ALLOWED and v not in (None, '', False)}

        View = request.env['ir.ui.view'].sudo()
        target_view = View.browse(int(view_id))
        if not target_view.exists():
            raise UserError(_('Target view not found.'))

        view_name = 'studio_lite.attrs.%s.%s' % (
            model.replace('.', '_'), field_name)
        existing = View.search([
            ('name', '=', view_name),
            ('model', '=', model),
            ('inherit_id', '=', target_view.id),
        ], limit=1)

        if not safe_attrs:
            if existing:
                existing.unlink()
            return {'success': True}

        data = etree.Element('data')
        xpath_el = etree.SubElement(data, 'xpath')
        xpath_el.set('expr', "//field[@name='%s']" % field_name)
        xpath_el.set('position', 'attributes')
        for attr_name, attr_val in safe_attrs.items():
            attr = etree.SubElement(xpath_el, 'attribute')
            attr.set('name', attr_name)
            attr.text = str(attr_val)

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)
        if existing:
            existing.write({'arch_db': arch})
        else:
            View.create({
                'name': view_name,
                'model': model,
                'inherit_id': target_view.id,
                'arch_db': arch,
                'priority': 9999,
            })
        return {'success': True}

    # ------------------------------------------------------------------
    # Rename a section (group @string)
    # ------------------------------------------------------------------

    @http.route('/studio_lite/rename_section', type='json', auth='user')
    def rename_section(self, model, view_id, section_xpath, new_title=''):
        self._check_access()
        self._validate_name(model, 'model')

        View = request.env['ir.ui.view'].sudo()
        target_view = View.browse(int(view_id))
        if not target_view.exists():
            raise UserError(_('Target view not found.'))

        data = etree.Element('data')
        xpath_el = etree.SubElement(data, 'xpath')
        xpath_el.set('expr', section_xpath)
        xpath_el.set('position', 'attributes')
        attr = etree.SubElement(xpath_el, 'attribute')
        attr.set('name', 'string')
        attr.text = (new_title or '').strip()

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)
        safe = re.sub(r'[^a-z0-9]', '_', section_xpath)[:30]
        view = View.create({
            'name': 'studio_lite.renamesec.%s.%s' % (
                model.replace('.', '_'), safe),
            'model': model,
            'inherit_id': target_view.id,
            'arch_db': arch,
            'priority': 9999,
        })

        ir_model = request.env['ir.model'].sudo().search(
            [('model', '=', model)], limit=1)
        request.env['studio.customization'].sudo().create({
            'name': _('Renamed section at %s on %s', section_xpath, model),
            'model_id': ir_model.id if ir_model else False,
            'customization_type': 'field_insert',
            'view_id': view.id,
            'target_view_id': target_view.id,
            'technical_data': json.dumps({
                'type': 'section_rename',
                'model': model,
                'section_xpath': section_xpath,
                'new_title': new_title,
                'arch': arch,
            }),
        })
        return {'success': True}

    # ------------------------------------------------------------------
    # Rename a tab / notebook page (@string)
    # ------------------------------------------------------------------

    @http.route('/studio_lite/rename_tab', type='json', auth='user')
    def rename_tab(self, model, view_id, tab_xpath, new_title=''):
        self._check_access()
        self._validate_name(model, 'model')

        View = request.env['ir.ui.view'].sudo()
        target_view = View.browse(int(view_id))
        if not target_view.exists():
            raise UserError(_('Target view not found.'))

        data = etree.Element('data')
        xpath_el = etree.SubElement(data, 'xpath')
        xpath_el.set('expr', tab_xpath)
        xpath_el.set('position', 'attributes')
        attr = etree.SubElement(xpath_el, 'attribute')
        attr.set('name', 'string')
        attr.text = (new_title or '').strip()

        arch = etree.tostring(data, encoding='unicode', pretty_print=True)
        safe = re.sub(r'[^a-z0-9]', '_', tab_xpath)[:30]
        view = View.create({
            'name': 'studio_lite.renametab.%s.%s' % (
                model.replace('.', '_'), safe),
            'model': model,
            'inherit_id': target_view.id,
            'arch_db': arch,
            'priority': 9999,
        })

        ir_model = request.env['ir.model'].sudo().search(
            [('model', '=', model)], limit=1)
        request.env['studio.customization'].sudo().create({
            'name': _('Renamed tab at %s on %s', tab_xpath, model),
            'model_id': ir_model.id if ir_model else False,
            'customization_type': 'field_insert',
            'view_id': view.id,
            'target_view_id': target_view.id,
            'technical_data': json.dumps({
                'type': 'tab_rename',
                'model': model,
                'tab_xpath': tab_xpath,
                'new_title': new_title,
                'arch': arch,
            }),
        })
        return {'success': True}

    # ------------------------------------------------------------------
    # Undo last Studio Lite action on this model
    # ------------------------------------------------------------------

    @http.route('/studio_lite/undo_last', type='json', auth='user')
    def undo_last(self, model, view_id):
        self._check_access()
        self._validate_name(model, 'model')

        ir_model = request.env['ir.model'].sudo().search(
            [('model', '=', model)], limit=1)
        if not ir_model:
            return {'success': False, 'message': _('Model not found.')}

        latest = request.env['studio.customization'].sudo().search([
            ('model_id', '=', ir_model.id),
            ('state', '=', 'active'),
        ], order='id desc', limit=1)

        if not latest:
            return {'success': False, 'message': _('Nothing to undo.')}

        undone_name = latest.name
        if latest.view_id and latest.view_id.exists():
            latest.view_id.unlink()
        latest.write({'state': 'reverted'})

        return {'success': True, 'undone': undone_name}
