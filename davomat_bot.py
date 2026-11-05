"""Ko'p guruhli davomat boti. Python 3.12+, aiogram 3.x.
BOT_TOKEN va ADMIN_IDS muhit o'zgaruvchilarini belgilang.
Railway Volume: /data. Bitta replica ishlating.
"""
import asyncio
import calendar
import logging
import os
import re
import sqlite3
import tempfile
import unicodedata
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from openpyxl.styles import Font, PatternFill
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile

TZ = timezone(timedelta(hours=5))
DB = Path(os.getenv('DB_PATH', str(Path(os.getenv('RAILWAY_VOLUME_MOUNT_PATH', '.')) / 'davomat_v2.sqlite3')))
ADMINS = set()
LOG = logging.getLogger('davomat')
dp = Dispatcher(storage=MemoryStorage())


def today():
    return datetime.now(TZ).date()


def norm(text):
    text = unicodedata.normalize('NFKC', text).casefold()
    return ' '.join(re.findall(r'[^\W_]+', re.sub(r"['‘’ʻʼ`]", '', text)))


def connection():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con


def sql(query, args=(), write=False):
    with closing(connection()) as con, con:
        cur = con.execute(query, args)
        return cur.lastrowid if write else [dict(row) for row in cur.fetchall()]


async def db(query, args=(), write=False):
    return await asyncio.to_thread(sql, query, args, write)


def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(connection()) as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('''
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, chat_id INTEGER NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id),
            full_name TEXT NOT NULL, name_key TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE UNIQUE INDEX IF NOT EXISTS active_names ON students(group_id,name_key) WHERE active=1;
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY, student_id INTEGER NOT NULL REFERENCES students(id),
            date TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('Kelgan','Kelmagan')),
            full_name TEXT NOT NULL, UNIQUE(student_id,date));
        CREATE INDEX IF NOT EXISTS dates ON attendance(date);
        CREATE TABLE IF NOT EXISTS submissions (
            group_id INTEGER NOT NULL REFERENCES groups(id), date TEXT NOT NULL,
            source_id INTEGER NOT NULL, PRIMARY KEY(group_id,date));
        ''')


def kb(items):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t,callback_data=c) for t,c in row] for row in items])


def home():
    return kb([[('➕ Guruh qo‘shish','newgroup')],[('📚 Guruhlar','groups:0')]])


def back(gid=0):
    return kb([[('🔙 Orqaga', f'g:{gid}' if gid else 'home')]])


def group_menu(gid):
    return kb([[(title,f'{action}:{gid}')] for title,action in [
        ('✏️ Guruh nomi','rename'),('🔗 Telegram IDni tahrirlash','chat'),
        ('➕ O‘quvchi qo‘shish','add'),('📋 O‘quvchilar / tahrirlash / o‘chirish','students'),
        ('✅ Davomat olish','take'),('📊 Excel hisobot','reports')]]+[[('🔙 Guruhlar','groups:0')]])


class Form(StatesGroup):
    value = State()


class Access(BaseMiddleware):
    async def __call__(self, handler, event, data):
        # Ulangan guruhdagi oddiy ro'yxatlarni istalgan a'zo yuborishi mumkin.
        # Admin paneli va /start uchun avvalgi ruxsat tekshiruvi saqlanadi.
        if isinstance(event, Message) and event.chat.type in {'group','supergroup'}:
            if event.text and not event.text.startswith('/'):
                linked = await db('SELECT id FROM groups WHERE chat_id=?', (event.chat.id,))
                if linked:
                    return await handler(event, data)
                return
        user = event.from_user
        if not user or user.id not in ADMINS:
            if isinstance(event, CallbackQuery):
                await event.answer('Ruxsat yo‘q.', show_alert=True)
            elif event.chat.type == 'private':
                await event.answer('Faqat belgilangan adminlar foydalanishi mumkin.')
            return
        if isinstance(event, CallbackQuery):
            if not isinstance(event.message, Message) or event.message.chat.type != 'private':
                await event.answer('Botning shaxsiy chatidan foydalaning.')
                return
        elif event.chat.type != 'private' and event.sender_chat:
            return
        return await handler(event, data)


dp.message.outer_middleware(Access())
dp.callback_query.outer_middleware(Access())


async def ask(message, state, mode, prompt, **values):
    await state.set_state(Form.value)
    await state.set_data(dict(mode=mode, **values))
    await message.answer(prompt, reply_markup=back(values.get('gid',0)))


@dp.message(CommandStart(), F.chat.type == 'private')
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('Davomat boshqaruvi', reply_markup=home())


async def roster(gid):
    return await db('SELECT * FROM students WHERE group_id=? AND active=1 ORDER BY full_name,id',(gid,))


@dp.callback_query()
async def click(query: CallbackQuery, state: FSMContext):
    await query.answer()
    parts = (query.data or '').split(':')
    action = parts[0]
    m = query.message
    # Davomat tasdiqlash tugmasi faqat ayni FSM loyihasiga tegishli.
    if action == 'save':
        data = await state.get_data()
        if data.get('mode') != 'confirm' or parts[1:] != [data.get('nonce')]:
            await m.answer('Bu tasdiqlash eskirgan. Davomatni qayta boshlang.')
            return
        result = await asyncio.to_thread(save_attendance,data['gid'],data['day'],data['present'],data['roster'],0)
        await state.clear()
        await m.answer(result,reply_markup=group_menu(data['gid']))
        return
    await state.clear()
    if action == 'home':
        await m.answer('Bosh menyu',reply_markup=home())
        return
    if action == 'newgroup':
        await ask(m,state,'group_name','Yangi guruh nomini kiriting (1–100 belgi):')
        return
    if action == 'groups':
        page = int(parts[1])
        rows = await db('SELECT * FROM groups ORDER BY id LIMIT 11 OFFSET ?',(page*10,))
        buttons = [[(r['name'],f"g:{r['id']}")] for r in rows[:10]]
        if page: buttons.append([('⬅️',f'groups:{page-1}')])
        if len(rows)>10: buttons.append([('➡️',f'groups:{page+1}')])
        buttons.append([('🔙 Bosh menyu','home')])
        await m.answer('Guruhni tanlang:' if rows else 'Guruhlar yo‘q.',reply_markup=kb(buttons))
        return
    if len(parts)<2 or not parts[1].isdigit(): return
    gid = int(parts[1])
    groups = await db('SELECT * FROM groups WHERE id=?',(gid,))
    if not groups:
        await m.answer('Guruh topilmadi.',reply_markup=home())
        return
    group = groups[0]
    if action == 'g':
        await m.answer(f"{group['name']}\nTelegram ID: {group['chat_id']}",reply_markup=group_menu(gid))
    elif action in {'rename','chat','add'}:
        prompt = {'rename':'Yangi guruh nomi:', 'chat':'Telegram guruh IDsi (-100…):','add':'O‘quvchi ism-familiyasi:'}[action]
        await ask(m,state,action,prompt,gid=gid)
    elif action == 'students':
        page = int(parts[2]) if len(parts)>2 else 0
        people = await roster(gid)
        buttons = [[(p['full_name'],f"student:{gid}:{p['id']}")] for p in people[page*10:page*10+10]]
        if page: buttons.append([('⬅️',f'students:{gid}:{page-1}')])
        if len(people)>(page+1)*10: buttons.append([('➡️',f'students:{gid}:{page+1}')])
        buttons.append([('🔙 Orqaga',f'g:{gid}')])
        await m.answer(f"O‘quvchilar: {len(people)}",reply_markup=kb(buttons))
    elif action in {'student','edit','delete','remove'}:
        sid = int(parts[2])
        rows = await db('SELECT * FROM students WHERE id=? AND group_id=? AND active=1',(sid,gid))
        if not rows:
            await m.answer('O‘quvchi topilmadi.',reply_markup=back(gid))
            return
        if action == 'student':
            await m.answer(rows[0]['full_name'],reply_markup=kb([[('✏️ Tahrirlash',f'edit:{gid}:{sid}')],[('❌ O‘chirish',f'delete:{gid}:{sid}')],[('🔙 Orqaga',f'students:{gid}')]]))
        elif action == 'edit':
            await ask(m,state,'edit','Yangi ism-familiya:',gid=gid,sid=sid)
        elif action == 'delete':
            await m.answer('Faol ro‘yxatdan o‘chirilsinmi? Davomat tarixi saqlanadi.',reply_markup=kb([[('✅ Ha',f'remove:{gid}:{sid}')],[('🔙 Bekor qilish',f'g:{gid}')]]))
        else:
            await db('UPDATE students SET active=0 WHERE id=? AND group_id=?',(sid,gid),True)
            await m.answer('O‘chirildi.',reply_markup=group_menu(gid))
    elif action == 'take':
        await m.answer('Davomat sanasi:',reply_markup=kb([[('📅 Bugun',f'attoday:{gid}')],[('🗓 Sana kiritish',f'atdate:{gid}')],[('🔙 Orqaga',f'g:{gid}')]]))
    elif action in {'attoday','atdate'}:
        if action == 'atdate':
            await ask(m,state,'atdate','Sana: YYYY-MM-DD (masalan, 2026-11-05).',gid=gid)
        else:
            await attendance_prompt(m,state,gid,today().isoformat())
    elif action == 'reports':
        await m.answer('Davrni tanlang. Keyin kerakli sana kiritiladi:',reply_markup=kb([[(label,f'period:{gid}:{period}')] for label,period in [('📅 Kunlik','day'),('📆 Haftalik','week'),('🗓 Oylik','month'),('📈 Yillik','year'),('📐 Ixtiyoriy sana oralig‘i','range')]]+[[('🔙 Orqaga',f'g:{gid}')]]))
    elif action == 'period':
        period = parts[2]
        if period not in {'day','week','month','year','range'}: return
        await ask(m,state,'report','YYYY-MM-DD formatida sana kiriting. Shu sana tegishli davr olinadi.' if period!='range' else 'Boshlanish va tugash sanasi: 2026-11-01 2026-11-30',gid=gid,period=period)


async def attendance_prompt(m,state,gid,day):
    people = await roster(gid)
    if not people:
        await m.answer('Avval o‘quvchi qo‘shing.',reply_markup=back(gid))
        return
    await ask(m,state,'names',f'{day} uchun kelganlarning to‘liq ism-familiyalarini har qatorda bittadan yuboring. Hech kim kelmasa: 0. Bu sana bo‘yicha oldingi davomat almashtiriladi.',gid=gid,day=day)


def parse_date(text):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',text): raise ValueError
    return date.fromisoformat(text)


def match_names(text, people):
    keys = {p['name_key']:p['id'] for p in people}
    found,unknown = set(),[]
    if text.strip() == '0': return found,unknown
    for line in text.splitlines():
        line = re.sub(r'^\s*(?:\d+[.)-]|[-•])\s*','',line).strip()
        if not line: continue
        key = norm(line)
        if key in keys: found.add(keys[key])
        else: unknown.append(line)
    return found,unknown


@dp.message(Form.value, F.chat.type == 'private')
async def form(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    mode = data.get('mode')
    gid = data.get('gid',0)
    value = (message.text or '').strip()
    try:
        if mode in {'group_name','rename'}:
            if not 1<=len(value)<=100: raise ValueError('Nom 1–100 belgidan iborat bo‘lsin.')
            if mode=='group_name':
                await ask(message,state,'group_chat','Telegram guruh IDsi (-100…):',name=value)
                return
            await db('UPDATE groups SET name=? WHERE id=?',(value,gid),True)
        elif mode in {'group_chat','chat'}:
            if not re.fullmatch(r'-\d+',value): raise ValueError('Manfiy raqamli guruh IDsini kiriting.')
            chat_id = int(value)
            chat = await bot.get_chat(chat_id)
            if chat.type not in {'group','supergroup'}: raise ValueError('Bu Telegram guruh emas.')
            member = await bot.get_chat_member(chat_id,(await bot.get_me()).id)
            if member.status not in {'administrator','creator'}: raise ValueError('Avval botni guruhga admin qiling.')
            if mode=='group_chat':
                gid = await db('INSERT INTO groups(name,chat_id) VALUES (?,?)',(data['name'],chat_id),True)
            else:
                await db('UPDATE groups SET chat_id=? WHERE id=?',(chat_id,gid),True)
        elif mode in {'add','edit'}:
            value = ' '.join(value.split())
            if not 3<=len(value)<=100 or len(norm(value).split())<2 or not all(c.isalpha() or c in " '-‘’ʻʼ`" for c in value):
                raise ValueError('Ism-familiyani harflarda, 3–100 belgida kiriting.')
            if mode=='add':
                await db('INSERT INTO students(group_id,full_name,name_key) VALUES (?,?,?)',(gid,value,norm(value)),True)
            else:
                await db('UPDATE students SET full_name=?,name_key=? WHERE id=? AND group_id=? AND active=1',(value,norm(value),data['sid'],gid),True)
        elif mode=='atdate':
            day = parse_date(value)
            if day>today(): raise ValueError('Kelajak sanasiga davomat yozilmaydi.')
            await attendance_prompt(message,state,gid,str(day))
            return
        elif mode=='names':
            people = await roster(gid)
            found,unknown = match_names(value,people)
            if unknown: raise ValueError('Bazadan topilmadi: '+', '.join(unknown)[:1500]+'. To‘liq ro‘yxatni tuzatib qayta yuboring.')
            if not value: raise ValueError('Ro‘yxat yoki 0 yuboring.')
            nonce = os.urandom(6).hex()
            await state.set_data(dict(mode='confirm',gid=gid,day=data['day'],present=list(found),roster=[(p['id'],p['full_name']) for p in people],nonce=nonce))
            await message.answer(f"{data['day']}: kelgan {len(found)}, kelmagan {len(people)-len(found)}. Saqlansinmi?",reply_markup=kb([[('✅ Saqlash','save:'+nonce)],[('🔙 Bekor qilish',f'g:{gid}')]]))
            return
        elif mode=='report':
            period = data['period']
            if period=='range':
                values = value.split()
                if len(values)!=2: raise ValueError('Ikkita sana kiriting.')
                start,end = map(parse_date,values)
            else:
                d = parse_date(value)
                start,end = {'day':(d,d),'week':(d-timedelta(days=d.weekday()),d+timedelta(days=6-d.weekday())), 'month':(d.replace(day=1),d.replace(day=calendar.monthrange(d.year,d.month)[1])), 'year':(d.replace(month=1,day=1),d.replace(month=12,day=31))}[period]
            if start>end or (end-start).days>730: raise ValueError('Oraliq tartibli va 731 kundan oshmagan bo‘lsin.')
            await message.answer('Excel tayyorlanmoqda…')
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder)/f'davomat_{gid}_{start}_{end}.xlsx'
                await asyncio.to_thread(report,gid,start,end,path)
                await message.answer_document(FSInputFile(path),caption='—: qayd etilmagan; foiz faqat qayd etilgan kunlar bo‘yicha.')
        else:
            await message.answer('Tasdiqlash yoki bekor qilish tugmasini bosing.')
            return
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        LOG.warning('Telegram guruhga kirishni rad etdi: %s', exc.message)
        await message.answer(
            'Guruh topilmadi yoki bot unga kira olmayapti.\n'
            '1. Aynan shu botni guruhga qo‘shib, admin qiling.\n'
            '2. Guruhda /start yuboring va bot ko‘rsatgan IDni nusxalang.\n'
            '3. Shu yerga to‘liq manfiy IDni qayta yuboring.\n'
            'Taklif havolasi yoki telefon raqamini kiritmang.',
            reply_markup=back(gid))
        return
    except TelegramNetworkError:
        await message.answer('Telegram bilan aloqa uzildi. Birozdan so‘ng qayta yuboring.', reply_markup=back(gid))
        return
    except (ValueError,sqlite3.IntegrityError) as exc:
        text = 'Bunday guruh IDsi yoki ism-familiya mavjud.' if isinstance(exc,sqlite3.IntegrityError) else str(exc) or 'Sana noto‘g‘ri. YYYY-MM-DD formatidan foydalaning.'
        await message.answer(text,reply_markup=back(gid))
        return
    await state.clear()
    await message.answer('Tayyor.',reply_markup=group_menu(gid) if gid else home())


def save_attendance(gid,day,present,expected,source_id):
    # Barcha o'quvchilar bitta tranzaksiyada yoziladi. Tarixiy ism ham saqlanadi.
    with closing(connection()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        current = [(r['id'],r['full_name']) for r in con.execute('SELECT id,full_name FROM students WHERE group_id=? AND active=1 ORDER BY full_name,id',(gid,))]
        if current != [tuple(x) for x in expected]: return 'O‘quvchilar ro‘yxati o‘zgargan. Davomatni qayta boshlang.'
        old = con.execute('SELECT source_id FROM submissions WHERE group_id=? AND date=?',(gid,day)).fetchone()
        if source_id and old and old[0]>=source_id: return 'Bu xabar avval qayta ishlangan.'
        if not current: return 'O‘quvchilar yo‘q.'
        con.executemany('''INSERT INTO attendance(student_id,date,status,full_name) VALUES (?,?,?,?)
        ON CONFLICT(student_id,date) DO UPDATE SET status=excluded.status,full_name=excluded.full_name''',[(sid,day,'Kelgan' if sid in present else 'Kelmagan',name) for sid,name in current])
        con.execute('''INSERT INTO submissions VALUES (?,?,?) ON CONFLICT(group_id,date)
        DO UPDATE SET source_id=MAX(submissions.source_id,excluded.source_id)''',(gid,day,source_id))
        return f'{day}: saqlandi. Kelgan: {len(present)}. Kelmagan: {len(current)-len(present)}.'


@dp.message(CommandStart(), F.chat.type.in_({'group','supergroup'}))
async def group_info(message: Message):
    # Guruh hali bazada bo'lmasa ham belgilangan admin aniq IDni oladi.
    await message.answer(
        f'Guruh: {message.chat.title}\nTelegram ID: {message.chat.id}\n'
        'Shu IDni botning shaxsiy chatidagi guruh qo‘shish paneliga kiriting.')


def auto_attendance(gid, day, text):
    # Bir kunda yuborilgan ro'yxatlar birlashtiriladi; takroriy xabar zarar qilmaydi.
    with closing(connection()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        people = [dict(r) for r in con.execute(
            'SELECT * FROM students WHERE group_id=? AND active=1 ORDER BY full_name,id', (gid,))]
        found, unknown = match_names(text, people)
        if not found:
            return None  # Oddiy suhbat yoki noma'lum ism davomatni o'zgartirmaydi.
        for person in people:
            status = 'Kelgan' if person['id'] in found else 'Kelmagan'
            con.execute('''INSERT INTO attendance(student_id,date,status,full_name) VALUES (?,?,?,?)
                ON CONFLICT(student_id,date) DO UPDATE SET
                status=CASE WHEN attendance.status='Kelgan' OR excluded.status='Kelgan'
                    THEN 'Kelgan' ELSE 'Kelmagan' END''',
                (person['id'], day, status, person['full_name']))
        present = con.execute('''SELECT COUNT(*) FROM attendance a JOIN students s ON s.id=a.student_id
            WHERE s.group_id=? AND s.active=1 AND a.date=? AND a.status='Kelgan' ''', (gid,day)).fetchone()[0]
        return present, len(people), len(found)


@dp.message(F.chat.type.in_({'group','supergroup'}), F.text)
async def group_message(message: Message):
    rows = await db('SELECT id FROM groups WHERE chat_id=?',(message.chat.id,))
    if not rows or message.text.startswith('/'):
        return
    # Sana xabarning Telegramdagi yuborilish vaqtidan, Toshkent vaqti bilan olinadi.
    day = message.date.astimezone(TZ).date().isoformat()
    # Guruhga hech qanday tasdiq yuborilmaydi; natija faqat bazada saqlanadi.
    await asyncio.to_thread(auto_attendance, rows[0]['id'], day, message.text)

@dp.message(F.chat.type == 'private')
async def fallback(message: Message):
    await message.answer('Menyudan tanlang:',reply_markup=home())


def report(gid,start,end,path):
    with closing(connection()) as con:
        con.execute('BEGIN')
        group = con.execute('SELECT name FROM groups WHERE id=?',(gid,)).fetchone()[0]
        people = con.execute('''SELECT id,full_name FROM students WHERE group_id=? AND (active=1 OR id IN
        (SELECT student_id FROM attendance WHERE date BETWEEN ? AND ?)) ORDER BY full_name,id''',(gid,str(start),str(end))).fetchall()
        records = con.execute('''SELECT a.* FROM attendance a JOIN students s ON s.id=a.student_id
        WHERE s.group_id=? AND a.date BETWEEN ? AND ? ORDER BY a.date,s.id''',(gid,str(start),str(end))).fetchall()
    days = [str(start+timedelta(days=i)) for i in range((end-start).days+1)]
    lookup = {(r['student_id'],r['date']):r['status'] for r in records}
    rows = []
    for p in people:
        values = [lookup.get((p['id'],day),'—') for day in days]
        yes,no = values.count('Kelgan'),values.count('Kelmagan')
        rows.append([p['id'],p['full_name'],yes,no,yes/(yes+no) if yes+no else None,*values])
    with pd.ExcelWriter(path,engine='openpyxl') as writer:
        pd.DataFrame(rows,columns=['ID','Ism-familiya (joriy)','Kelgan','Kelmagan','Davomat %',*days]).to_excel(writer,index=False,sheet_name='Davomat')
        pd.DataFrame([[r['student_id'],r['full_name'],r['date'],r['status']] for r in records],columns=['ID','Ism-familiya (qayd vaqtida)','Sana','Holat']).to_excel(writer,index=False,sheet_name='Tafsilot')
        pd.DataFrame([['Guruh',group],['Boshlanish',str(start)],['Tugash',str(end)],['Izoh','—: qayd yo‘q. Hafta: dushanba–yakshanba.']],columns=['Maydon','Qiymat']).to_excel(writer,index=False,sheet_name='Ma’lumot')
        for ws in writer.book.worksheets:
            ws.freeze_panes = 'C2'
            ws.auto_filter.ref = ws.dimensions
            for col in ws.columns: ws.column_dimensions[col[0].column_letter].width = 34 if col[0].column==2 else 14
            for c in ws[1]:
                c.font = Font(bold=True,color='FFFFFF')
                c.fill = PatternFill('solid',fgColor='17365D')
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    if isinstance(cell.value,str): cell.data_type='s'
                    if cell.value in ('Kelgan','Kelmagan'):
                        cell.fill=PatternFill('solid',fgColor='E2F0D9' if cell.value=='Kelgan' else 'FCE4D6')
            if ws.title=='Davomat':
                for row in ws.iter_rows(min_row=2): row[4].number_format='0.0%'


@dp.errors()
async def error(event):
    LOG.error('Amal xatosi',exc_info=(type(event.exception),event.exception,event.exception.__traceback__))
    m = event.update.message or (event.update.callback_query.message if event.update.callback_query else None)
    if isinstance(m,Message) and m.chat.type == 'private':
        try: await m.answer('Amal bajarilmadi. Guruh IDsi, botning admin huquqi va ulanishni tekshirib, qayta urinib ko‘ring.')
        except Exception: LOG.exception('Xato xabari yuborilmadi')
    return True


async def main():
    global ADMINS
    token = os.getenv('BOT_TOKEN','').strip()
    try:
        ADMINS = {int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip()}
        if not token or not ADMINS or min(ADMINS)<=0: raise ValueError
    except ValueError:
        raise SystemExit('BOT_TOKEN va ADMIN_IDS ni sozlang.')
    await asyncio.to_thread(initialize)
    async with Bot(token) as bot:
        await bot.delete_webhook(drop_pending_updates=False)
        # Ketma-ket qayta ishlash: parallel yozuv va FSM poygasini cheklaydi.
        await dp.start_polling(bot,handle_as_tasks=False,close_bot_session=False)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    try: asyncio.run(main())
    except KeyboardInterrupt: pass



