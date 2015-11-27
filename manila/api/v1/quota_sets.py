# Copyright 2011 OpenStack LLC.
# Copyright (c) 2015 Mirantis inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_log import log
from oslo_utils import strutils
from six.moves.urllib import parse
import webob

from manila.api.openstack import wsgi
from manila.api.views import quota_sets as quota_sets_views
from manila import db
from manila import exception
from manila.i18n import _
from manila import quota

QUOTAS = quota.QUOTAS
LOG = log.getLogger(__name__)
NON_QUOTA_KEYS = ('tenant_id', 'id', 'force')


class QuotaSetsController(wsgi.Controller):
    """The Quota Sets API controller for the OpenStack API."""

    resource_name = "quota_set"
    _view_builder_class = quota_sets_views.ViewBuilder

    def _validate_quota_limit(self, limit, minimum, maximum, force_update):
        # NOTE: -1 is a flag value for unlimited
        if limit < -1:
            msg = _("Quota limit must be -1 or greater.")
            raise webob.exc.HTTPBadRequest(explanation=msg)
        if ((limit < minimum and not force_update) and
           (maximum != -1 or (maximum == -1 and limit != -1))):
            msg = _("Quota limit must be greater than %s.") % minimum
            raise webob.exc.HTTPBadRequest(explanation=msg)
        if maximum != -1 and limit > maximum:
            msg = _("Quota limit must be less than %s.") % maximum
            raise webob.exc.HTTPBadRequest(explanation=msg)

    def _get_quotas(self, context, id, user_id=None, usages=False):
        if user_id:
            values = QUOTAS.get_user_quotas(context, id, user_id,
                                            usages=usages)
        else:
            values = QUOTAS.get_project_quotas(context, id, usages=usages)

        if usages:
            return values
        return dict((k, v['limit']) for k, v in values.items())

    @wsgi.Controller.authorize
    def show(self, req, id):
        context = req.environ['manila.context']
        params = parse.parse_qs(req.environ.get('QUERY_STRING', ''))
        user_id = params.get('user_id', [None])[0]
        try:
            db.authorize_project_context(context, id)
            return self._view_builder.detail_list(
                self._get_quotas(context, id, user_id=user_id), id)
        except exception.NotAuthorized:
            raise webob.exc.HTTPForbidden()

    @wsgi.Controller.authorize('show')
    def defaults(self, req, id):
        context = req.environ['manila.context']
        return self._view_builder.detail_list(QUOTAS.get_defaults(context), id)

    @wsgi.Controller.authorize
    def update(self, req, id, body):
        context = req.environ['manila.context']
        project_id = id
        bad_keys = []
        force_update = False
        params = parse.parse_qs(req.environ.get('QUERY_STRING', ''))
        user_id = params.get('user_id', [None])[0]

        try:
            settable_quotas = QUOTAS.get_settable_quotas(context, project_id,
                                                         user_id=user_id)
        except exception.NotAuthorized:
            raise webob.exc.HTTPForbidden()

        for key, value in body.get('quota_set', {}).items():
            if (key not in QUOTAS and
                    key not in NON_QUOTA_KEYS):
                bad_keys.append(key)
                continue
            if key == 'force':
                force_update = strutils.bool_from_string(value)
            elif key not in NON_QUOTA_KEYS and value:
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    msg = _("Quota '%(value)s' for %(key)s should be "
                            "integer.") % {'value': value, 'key': key}
                    LOG.warn(msg)
                    raise webob.exc.HTTPBadRequest(explanation=msg)

        LOG.debug("Force update quotas: %s.", force_update)

        if len(bad_keys) > 0:
            msg = _("Bad key(s) %s in quota_set.") % ",".join(bad_keys)
            raise webob.exc.HTTPBadRequest(explanation=msg)

        try:
            quotas = self._get_quotas(context, id, user_id=user_id,
                                      usages=True)
        except exception.NotAuthorized:
            raise webob.exc.HTTPForbidden()

        for key, value in body.get('quota_set', {}).items():
            if key in NON_QUOTA_KEYS or (not value and value != 0):
                continue
            # validate whether already used and reserved exceeds the new
            # quota, this check will be ignored if admin want to force
            # update
            try:
                value = int(value)
            except (ValueError, TypeError):
                msg = _("Quota '%(value)s' for %(key)s should be "
                        "integer.") % {'value': value, 'key': key}
                LOG.warn(msg)
                raise webob.exc.HTTPBadRequest(explanation=msg)

            if force_update is False and value >= 0:
                quota_value = quotas.get(key)
                if quota_value and quota_value['limit'] >= 0:
                    quota_used = (quota_value['in_use'] +
                                  quota_value['reserved'])
                    LOG.debug("Quota %(key)s used: %(quota_used)s, "
                              "value: %(value)s.",
                              {'key': key, 'quota_used': quota_used,
                               'value': value})
                    if quota_used > value:
                        msg = (_("Quota value %(value)s for %(key)s are "
                                 "greater than already used and reserved "
                                 "%(quota_used)s.") %
                               {'value': value, 'key': key,
                                'quota_used': quota_used})
                        raise webob.exc.HTTPBadRequest(explanation=msg)

            minimum = settable_quotas[key]['minimum']
            maximum = settable_quotas[key]['maximum']
            self._validate_quota_limit(value, minimum, maximum, force_update)
            try:
                db.quota_create(context, project_id, key, value,
                                user_id=user_id)
            except exception.QuotaExists:
                db.quota_update(context, project_id, key, value,
                                user_id=user_id)
            except exception.AdminRequired:
                raise webob.exc.HTTPForbidden()
        return self._view_builder.detail_list(
            self._get_quotas(context, id, user_id=user_id))

    @wsgi.Controller.authorize
    def delete(self, req, id):
        context = req.environ['manila.context']
        params = parse.parse_qs(req.environ.get('QUERY_STRING', ''))
        user_id = params.get('user_id', [None])[0]
        try:
            db.authorize_project_context(context, id)
            if user_id:
                QUOTAS.destroy_all_by_project_and_user(context, id, user_id)
            else:
                QUOTAS.destroy_all_by_project(context, id)
            return webob.Response(status_int=202)
        except exception.NotAuthorized:
            raise webob.exc.HTTPForbidden()


def create_resource():
    return wsgi.Resource(QuotaSetsController())
