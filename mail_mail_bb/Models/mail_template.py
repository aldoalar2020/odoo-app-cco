# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import babel
import base64
import copy
import datetime
import dateutil.relativedelta as relativedelta
import functools
import logging

from werkzeug import urls

from odoo import _, api, fields, models, tools
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)



class MailTemplate(models.Model):
    "Templates for sending email"
    _inherit = "mail.template"

    email_cco = fields.Char('Cco', help='Copia carbon oculta')

    def generate_email(self, res_ids, fields=None):
        """Generates an email from the template for given the given model based on
        records given by res_ids.

        :param res_id: id of the record to use for rendering the template (model
                       is taken from template definition)
        :returns: a dict containing all relevant fields for creating a new
                  mail.mail entry, with one extra key ``attachments``, in the
                  format [(report_name, data)] where data is base64 encoded.
        """
        self.ensure_one()
        multi_mode = True
        if isinstance(res_ids, int):
            res_ids = [res_ids]
            multi_mode = False
        if fields is None:
            fields = ['subject', 'body_html', 'email_from', 'email_to', 'partner_to', 'email_cc', 'reply_to', 'scheduled_date','email_cco']

        res_ids_to_templates = self.get_email_template(res_ids)

        # templates: res_id -> template; template -> res_ids
        templates_to_res_ids = {}
        for res_id, template in res_ids_to_templates.items():
            templates_to_res_ids.setdefault(template, []).append(res_id)

        results = dict()
        for template, template_res_ids in templates_to_res_ids.items():
            Template = self.env['mail.template']
            # generate fields value for all res_ids linked to the current template
            if template.lang:
                Template = Template.with_context(lang=template._context.get('lang'))
            for field in fields:
                Template = Template.with_context(safe=field in {'subject'})
                generated_field_values = Template._render_template(
                    getattr(template, field), template.model, template_res_ids,
                    post_process=(field == 'body_html'))
                for res_id, field_value in generated_field_values.items():
                    results.setdefault(res_id, dict())[field] = field_value
            # compute recipients
            if any(field in fields for field in ['email_to', 'partner_to', 'email_cc','email_cco']):
                results = template.generate_recipients(results, template_res_ids)
            # update values for all res_ids
            for res_id in template_res_ids:
                values = results[res_id]
                # body: add user signature, sanitize
                if 'body_html' in fields and template.user_signature:
                    signature = self.env.user.signature
                    if signature:
                        values['body_html'] = tools.append_content_to_html(values['body_html'], signature, plaintext=False)
                if values.get('body_html'):
                    values['body'] = tools.html_sanitize(values['body_html'])
                # technical settings
                values.update(
                    mail_server_id=template.mail_server_id.id or False,
                    auto_delete=template.auto_delete,
                    model=template.model,
                    res_id=res_id or False,
                    attachment_ids=[attach.id for attach in template.attachment_ids],
                )

            # Add report in attachments: generate once for all template_res_ids
            if template.report_template:
                for res_id in template_res_ids:
                    attachments = []
                    report_name = self._render_template(template.report_name, template.model, res_id)
                    report = template.report_template
                    report_service = report.report_name

                    if report.report_type in ['qweb-html', 'qweb-pdf']:
                        result, format = report.render_qweb_pdf([res_id])
                    else:
                        res = report.render([res_id])
                        if not res:
                            raise UserError(_('Unsupported report type %s found.') % report.report_type)
                        result, format = res

                    # TODO in trunk, change return format to binary to match message_post expected format
                    result = base64.b64encode(result)
                    if not report_name:
                        report_name = 'report.' + report_service
                    ext = "." + format
                    if not report_name.endswith(ext):
                        report_name += ext
                    attachments.append((report_name, result))
                    results[res_id]['attachments'] = attachments

        return multi_mode and results or results[res_ids[0]]

    # ----------------------------------------
    # EMAIL
    # ----------------------------------------

    def send_mail(self, res_id, force_send=False, raise_exception=False, email_values=None, notif_layout=False):
        """ Generates a new mail.mail. Template is rendered on record given by
        res_id and model coming from template.

        :param int res_id: id of the record to render the template
        :param bool force_send: send email immediately; otherwise use the mail
            queue (recommended);
        :param dict email_values: update generated mail with those values to further
            customize the mail;
        :param str notif_layout: optional notification layout to encapsulate the
            generated email;
        :returns: id of the mail.mail that was created """
        self.ensure_one()
        Mail = self.env['mail.mail']
        Attachment = self.env['ir.attachment']  # TDE FIXME: should remove default_type from context

        # create a mail_mail based on values, without attachments
        values = self.generate_email(res_id)
        values['recipient_ids'] = [(4, pid) for pid in values.get('partner_ids', list())]
        values['attachment_ids'] = [(4, aid) for aid in values.get('attachment_ids', list())]
        values.update(email_values or {})
        attachment_ids = values.pop('attachment_ids', [])
        attachments = values.pop('attachments', [])
        # add a protection against void email_from
        if 'email_from' in values and not values.get('email_from'):
            values.pop('email_from')
        # encapsulate body
        if notif_layout and values['body_html']:
            try:
                template = self.env.ref(notif_layout, raise_if_not_found=True)
            except ValueError:
                _logger.warning('QWeb template %s not found when sending template %s. Sending without layouting.' % (notif_layout, self.name))
            else:
                record = self.env[self.model].browse(res_id)
                lang = self._render_template(self.lang, self.model, res_id)
                model = self.env['ir.model']._get(record._name)
                if lang:
                    template = template.with_context(lang=lang)
                    model = model.with_context(lang=lang)
                template_ctx = {
                    'message': self.env['mail.message'].sudo().new(dict(body=values['body_html'], record_name=record.display_name)),
                    'model_description': model.display_name,
                    'company': 'company_id' in record and record['company_id'] or self.env.company,
                    'record': record,
                }
                body = template.render(template_ctx, engine='ir.qweb', minimal_qcontext=True)
                values['body_html'] = self.env['mail.thread']._replace_local_links(body)
        mail = Mail.create(values)

        # manage attachments
        for attachment in attachments:
            attachment_data = {
                'name': attachment[0],
                'datas': attachment[1],
                'type': 'binary',
                'res_model': 'mail.message',
                'res_id': mail.mail_message_id.id,
            }
            attachment_ids.append((4, Attachment.create(attachment_data).id))
        if attachment_ids:
            mail.write({'attachment_ids': attachment_ids})

        if force_send:
            mail.send(raise_exception=raise_exception)
        return mail.id  # TDE CLEANME: return mail + api.returns ?
