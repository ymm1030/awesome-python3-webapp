# -*- coding: utf-8 -*-

import asyncio
from coreweb import get, post
from model import User, Blog, Comment

@get('/')
async def index(request):
    users = await User.findAll()
    return {
        '__template__' : 'test.html',
        'users' : users
    }