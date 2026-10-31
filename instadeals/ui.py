"""Telegram-native navigation; commands remain available as shortcuts."""
from types import SimpleNamespace
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup, ReplyKeyboardMarkup


def keyboard(rows):
    return InlineKeyboardMarkup([[Button(label,callback_data='ui:'+action) for label,action in row] for row in rows])

HOME = ReplyKeyboardMarkup([['Menu','My locations'],['Discount settings','Monitoring status']],resize_keyboard=True,is_persistent=True)

class MenuUI:
    async def run_command(self, update, context, text):
        message = update.callback_query.message if update.callback_query else update.message
        proxy = SimpleNamespace(text=text,reply_text=message.reply_text)
        await self.command(SimpleNamespace(effective_user=update.effective_user,effective_chat=update.effective_chat,message=proxy),context)

    async def menu(self, uid, message, intro=False):
        user=self.db.user(uid)
        connected=bool(self.accounts and self.accounts.generation(uid))
        count=len(self.db.locations(uid))
        body=('Welcome to Instamart Deal Monitor\n\nConnect your Swiggy account, choose a saved delivery address, and set your minimum discount. I check prices regularly and send qualifying deals here.\n\n' if intro else 'Your deal monitor\n\n')
        body+=f"Swiggy: {'Connected' if connected else 'Not connected'}\nLocations: {count}\nOffer mode: {user['offer_mode']}\nMinimum discount: {user['threshold']}% (exceptional mode)\nAlerts: {'On' if user['enabled'] else 'Paused'}"
        rows=[]
        if not connected:
            rows.append([('Connect Swiggy','connect')])
        rows += [[('My locations','locations'),('Discount settings','discount')],
                 [('Monitoring status','status'),('Pause alerts' if user['enabled'] else 'Resume alerts','pause' if user['enabled'] else 'resume')],
                 [('Offer preferences','offers'),('Coverage & limits','coverage')],
                 [('Account','account'),('How it works','help')]]
        await message.reply_text(body,reply_markup=keyboard(rows))

    async def ui(self, update, context):
        query=update.callback_query
        uid=update.effective_user.id
        if not update.effective_chat or update.effective_chat.type!='private' or (not self.s.public_access and uid not in self.s.users):
            await query.answer('This menu is private.',show_alert=True)
            return
        await query.answer()
        action=query.data.removeprefix('ui:')
        await self.ui_action(update,context,action)

    async def ui_action(self, update, context, action):
        uid=update.effective_user.id
        message=update.callback_query.message if update.callback_query else update.message
        self.db.user(uid)
        self.ui_inputs.pop(uid,None)
        back=[[('Back to menu','home')]]
        if action=='home':
            await self.menu(uid,message)
        elif action=='locations':
            locations=self.db.locations(uid)
            rows=[[('Choose Swiggy addresses','addresses')]]
            body='My locations\n\nSelect up to 10 saved addresses. Each keeps its own prices and alerts. Adding one does not replace the others.\n'
            if not locations:
                body+='No locations selected yet. Choose an address saved in your Swiggy account.'
            for row in locations:
                linked=row['swiggy_address_id'] and self.accounts and row['swiggy_generation']==self.accounts.generation(uid)
                body+=f"\n{row['name']} — {'Saved Swiggy address' if linked else 'Needs a saved Swiggy address'}"
                rows.append([(f"Manage {row['name'][:35]}",f"location:{row['id']}")])
            await message.reply_text(body,reply_markup=keyboard(rows+back))
        elif action.startswith('location:'):
            try: loc_id=int(action.split(':')[1])
            except ValueError: return
            row=self.db.run('SELECT * FROM locations WHERE id=? AND user_id=?',(loc_id,uid)).fetchone()
            if not row:
                await message.reply_text('This location was removed.',reply_markup=keyboard(back));return
            await message.reply_text(f"{row['name']}\n\nChoose another saved address to add a location, or remove this one from monitoring.",reply_markup=keyboard([[('Choose another address','addresses')],[('Remove location',f'remove:{loc_id}')],[('Back to locations','locations')]]))
        elif action.startswith('remove:'):
            try: loc_id=int(action.split(':')[1])
            except ValueError: return
            self.db.run('DELETE FROM locations WHERE id=? AND user_id=?',(loc_id,uid))
            await message.reply_text('Location removed from monitoring. Your Swiggy address is unchanged.',reply_markup=keyboard([[('My locations','locations')]]))
        elif action=='offers':
            user=self.db.user(uid)
            rows=[[(('✓ ' if user['offer_mode']==mode else '')+label, 'mode:'+mode)]
                  for mode,label in [('exceptional','Exceptional discounts'),('all','All discovered offers')]]
            rows += [[(('✓ ' if user['sort_order']==order else '')+label, 'sort:'+order)]
                     for order,label in [('discount','Discount: high to low'),('price_low','Price: low to high'),('price_high','Price: high to low')]]
            await message.reply_text('Offer preferences\n\nExceptional: your discount threshold and minimum savings apply.\nAll discovered offers: any positive item discount or explicit buy-one/get-one or buy-two/get-one badge; no minimum ₹ savings. May send many alerts. Stock, stable observations and price-history checks still apply.\nSorting applies per address to newly qualifying alerts. Previously sent deals are not resent when you change sorting. Badge terms must be checked in Instamart.',reply_markup=keyboard(rows+back))
        elif action.startswith('mode:'):
            mode=action.split(':',1)[1]
            if mode not in ('exceptional','all'): return
            self.db.run('UPDATE users SET offer_mode=? WHERE id=?',(mode,uid))
            await self.ui_action(update,context,'offers')
        elif action.startswith('sort:'):
            order=action.split(':',1)[1]
            if order not in ('discount','price_low','price_high'): return
            self.db.run('UPDATE users SET sort_order=? WHERE id=?',(order,uid))
            await self.ui_action(update,context,'offers')
        elif action=='coverage':
            count=len(self.db.locations(uid))
            requests=len(self.s.queries)*self.s.pages
            await message.reply_text(f'Coverage & limits\n\nSearch terms: {len(self.s.queries)}\nPages per term: {self.s.pages} (operator setting; supported range 1–20)\nUp to {requests} search requests per address per scan\nSelected locations: {count}/10\nRequest spacing: {self.s.gap:g}s across users\nWait after scan: {self.s.poll}s\nStable observations required: {self.s.confirmations}\n\nSearch is bounded, not the entire catalog. More pages and addresses slow alerts. Some short-lived offers will be missed. Combo badges are shown only when returned; cart coupons, bank and membership offers are not monitored.',reply_markup=keyboard(back))
        elif action=='discount':
            current=self.db.user(uid)['threshold']
            rows=[[(f"{'✓ ' if current==value else ''}{value}%",f'discount:{value}') for value in (40,50,60)],[(f"{'✓ ' if current==value else ''}{value}%",f'discount:{value}') for value in (70,80,90)], [('Custom percentage','custom')]]
            await message.reply_text(f"Minimum discount: {current}% off MRP\n\nChoose a threshold for exceptional mode. In that mode, deals must also save at least ₹{self.s.savings/100:.0f} and pass availability and price-history checks.",reply_markup=keyboard(rows+back))
        elif action.startswith('discount:'):
            try: value=int(action.split(':')[1])
            except ValueError: return
            if 1<=value<=99:
                self.db.run('UPDATE users SET threshold=? WHERE id=?',(value,uid))
                await message.reply_text(f'Minimum discount saved: {value}%.',reply_markup=keyboard(back))
        elif action=='custom':
            import time
            self.ui_inputs[uid]=time.time()+300
            await message.reply_text('Send a whole number from 1 to 99 for your minimum discount. This prompt expires in 5 minutes.',reply_markup=keyboard([[('Cancel','cancel')]]))
        elif action=='cancel':
            self.ui_inputs.pop(uid,None)
            await self.menu(uid,message)
        elif action=='addresses':
            await message.reply_text('Loading your saved Swiggy addresses…')
            await self.show_addresses(uid,message)
        elif action=='connect':
            if not self.s.callback_url:
                await message.reply_text('Swiggy login is not configured yet. Contact the bot operator.',reply_markup=keyboard(back));return
            try:
                link=await self.accounts.begin(uid,self.s.callback_url)
                body='Connect your Swiggy account\n\nSign in on Swiggy using your phone and OTP. Then tap “Return to Telegram” on the confirmation page and press Start. Never send your OTP here.'
                if self.s.callback_url.startswith('http://localhost:'):
                    body+='\n\nLocal test: open the sign-in link in Brave on the Mac running this bot.'
                await message.reply_text(body,reply_markup=InlineKeyboardMarkup([[Button('Sign in to Swiggy',url=link)],[Button('Back to menu',callback_data='ui:home')]]))
            except Exception:
                await message.reply_text('Could not create a login link. Wait 30 seconds and try again.',reply_markup=keyboard([[('Try again','connect')]]+back))
        elif action in ('pause','resume'):
            self.db.run('UPDATE users SET enabled=? WHERE id=?',(int(action=='resume'),uid))
            await self.menu(uid,message)
        elif action=='status':
            status=self.user_health.get(uid,'Waiting for a connected Swiggy account and a selected delivery address.')
            await message.reply_text(status,reply_markup=keyboard([[('Choose addresses','addresses'),('Refresh','status')]]+back))
        elif action=='account':
            connected=bool(self.accounts and self.accounts.generation(uid))
            rows=[[('Reconnect Swiggy' if connected else 'Connect Swiggy','connect')]]
            if connected: rows.append([('Disconnect Swiggy','disconnect')])
            await message.reply_text('Your Swiggy account\n\n'+('Connected. Credentials are stored encrypted and kept separate from other users.' if connected else 'Not connected. Sign in to use your saved addresses.'),reply_markup=keyboard(rows+back))
        elif action=='disconnect':
            await message.reply_text('Disconnect Swiggy? Alerts will pause and saved credentials will be removed. Your monitored locations will remain.',reply_markup=keyboard([[('Disconnect','disconnect_yes'),('Keep connected','account')]]))
        elif action=='disconnect_yes':
            await self.run_command(update,context,'/disconnect')
            await self.menu(uid,message)
        elif action=='help':
            await message.reply_text(f"How it works\n\n1. Connect your Swiggy account.\n2. Choose one or more saved delivery addresses.\n3. Set a discount threshold and leave alerts on.\n\nI wait {self.s.poll//60} minutes between completed scans and require {self.s.confirmations} matching observations. No alert means no qualifying deal was found.\n\nSearch coverage: {', '.join(self.s.queries)}. Prices and stock can change before checkout. MRP discounts do not guarantee the lowest market price.",reply_markup=keyboard(back))
