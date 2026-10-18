"""Useful owner diagnostics without dumping request URLs or credentials."""
import errno
import os
import sqlite3
from .source import SourceError


def error_description(error, source=None):
    if isinstance(error,SourceError):
        detail = str(error)
    elif isinstance(error,OSError):
        detail = {errno.ENOSPC:'Недостаточно места на диске.',errno.EACCES:'Нет прав на запись в папку данных.',
                  errno.EROFS:'Файловая система доступна только для чтения.'}.get(error.errno,type(error).__name__+(': '+os.strerror(error.errno) if error.errno else ''))
    elif isinstance(error,sqlite3.Error):
        detail = 'Ошибка локальной базы: '+str(error)[:250]
    else:
        detail = type(error).__name__
    progress = getattr(source,'refresh_progress',{}) or {}
    if progress.get('phase') in ('prepare','save','check','publish','backup'):
        stage = progress.get('detail') or 'Сохранение локальной базы'
    else:
        request = progress.get('request',{})
        action = request.get('action')
        kind = {'group':'групп','teacher':'преподавателей','room':'аудиторий'}.get(request.get('type'),'')
        stage = {'period':'Учебный период','catalog':'Каталог '+kind,'schedule':'Расписание '+kind}.get(action,'')
        if action == 'schedule':stage += ', ID '+str(request.get('id','?'))
    return detail[:500]+('\nЭтап: '+stage if stage else '')
