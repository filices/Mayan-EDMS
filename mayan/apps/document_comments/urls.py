from __future__ import unicode_literals

from django.conf.urls import patterns, url

urlpatterns = patterns(
    'document_comments.views',
    url(
        r'^comment/(?P<comment_id>\d+)/delete/$', 'comment_delete',
        name='comment_delete'
    ),
    url(
        r'^comment/multiple/delete/$', 'comment_multiple_delete',
        name='comment_multiple_delete'
    ),
    url(
        r'^(?P<document_id>\d+)/comment/add/$', 'comment_add',
        name='comment_add'
    ),
    url(
        r'^(?P<document_id>\d+)/comment/list/$', 'comments_for_document',
        name='comments_for_document'
    ),
)
