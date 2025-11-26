from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
import os
import json
import uuid
from datetime import datetime, timedelta
import jwt

app= Flask(__name__)
app.config['SECRET_KEY']= 'supersecretjwt'
socketio= SocketIO(app, cors_allowed_origins='*')

DATA_DIR= os.path.join(os.path.abspath(os.path.dirname(__file__)),'data')
os.makedirs(DATA_DIR, exist_ok=True)

USERS_FILE= os.path.join(DATA_DIR,'users.json')
TASKS_FILE= os.path.join(DATA_DIR,'tasks.json')
TOPUPS_FILE= os.path.join(DATA_DIR,'topups.json')
WITHDRAWS_FILE= os.path.join(DATA_DIR,'withdraws.json')
REVIEWS_FILE= os.path.join(DATA_DIR,'reviews.json')
TASK_TYPES_FILE= os.path.join(DATA_DIR,'task_types.json')
ADMINS_FILE= os.path.join(DATA_DIR,'admins.json')

# helpers
def load_json(path,default): 
    if os.path.exists(path):
        with open(path,'r',encoding='utf-8') as f:
            return json.load(f)
    return default
def save_json(path,data): 
    with open(path,'w',encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def get_user(uid):
    users= load_json(USERS_FILE,{})
    if uid not in users:
        users[uid]= {'balance':0, 'tasks_done':0, 'history':[]}
        save_json(USERS_FILE, users)
    return users[uid]
def update_user_balance(uid,amount,note=''):
    users= load_json(USERS_FILE,{})
    user= get_user(uid)
    user['balance']+=amount
    user['balance']=round(user['balance'],2)
    user['history'].insert(0, {'amount':amount,'note':note,'date':datetime.utcnow().isoformat()+'Z'})
    users[uid]=user
    save_json(USERS_FILE,users)
    socketio.emit('user_update',{'user_id':uid,'balance':user['balance']})
    return user

# API
@app.route('/api/profile_me')
def api_profile():
    uid= request.args.get('uid')
    user= get_user(uid)
    return jsonify({'ok':True,'user':user})
@app.route('/api/tasks/list')
def api_tasks_list():
    tasks= load_json(TASKS_FILE,[])
    return jsonify({'ok':True,'tasks':tasks})
@app.route('/api/tasks/create', methods=['POST'])
def api_tasks_create():
    data= request.json
    task={
        'id':str(uuid.uuid4()),
        'title':data['title'],
        'description':data.get('description',''),
        'type_id': data.get('type_id','custom'),
        'unit_price': float(data['unit_price']),
        'qty': int(data['qty']),
        'done':0,
        'created_at': datetime.utcnow().isoformat()
    }
    tasks= load_json(TASKS_FILE,[])
    tasks.append(task)
    save_json(TASKS_FILE, tasks)
    socketio.emit('task_created', task)
    return jsonify({'ok':True,'task':task})
@app.route('/api/admin/users')
def api_admin_users():
    users= load_json(USERS_FILE,{})
    return jsonify({'ok':True, 'users':[{'id':k,**v} for k,v in users.items()]})
@app.route('/api/admin/topups')
def api_admin_topups():
    return jsonify({'ok':True,'items':load_json(TOPUPS_FILE,[])})
@app.route('/api/admin/withdraws')
def api_admin_withdraws():
    return jsonify({'ok':True,'items':load_json(WITHDRAWS_FILE,[])})
@app.route('/api/admin/types', methods=['GET','POST'])
def api_admin_types():
    if request.method=='GET':
        return jsonify({'ok':True,'types':load_json(TASK_TYPES_FILE,[])})
    else:
        data=request.json
        types= load_json(TASK_TYPES_FILE,[])
        new_type={
            'id': str(uuid.uuid4()),
            'name': data['name'],
            'price': float(data['price'])
        }
        types.append(new_type)
        save_json(TASK_TYPES_FILE,types)
        return jsonify({'ok':True,'type':new_type})

@app.route('/api/admin/dashboard')
def admin_dashboard():
    users= load_json(USERS_FILE,{})
    total_revenue= sum(v['balance'] for v in users.values())
    tasks= load_json(TASKS_FILE,[])
    pending= len([t for t in tasks if t['done']<t['qty']])
    return jsonify({'ok':True,'data':{'usersCount':len(users),'totalRevenue':total_revenue,'tasksCount':len(tasks),'pendingCount':pending}})

# подтверждение пополнений
@app.route('/api/admin/topup/<topup_id>/<action>', methods=['POST'])
def admin_topup_action(topup_id,action):
    items= load_json(TOPUPS_FILE,[])
    for t in items:
        if t['id']==topup_id:
            if action=='approve':
                t['status']='paid'
                update_user_balance(t['user_id'], t['amount'],'topup approved')
                socketio.emit('user_update',{'user_id': t['user_id'],'balance': get_user(t['user_id'])['balance']})
            elif action=='reject': t['status']='refunded'
            save_json(TOPUPS_FILE,items)
            return jsonify({'ok':True})
    return jsonify({'ok':False})
@app.route('/api/admin/withdraw/<wd_id>/<action>', methods=['POST'])
def admin_withdraw_action(wd_id,action):
    items= load_json(WITHDRAWS_FILE,[])
    for w in items:
        if w['id']==wd_id:
            if action=='approve':
                w['status']='approved'
                socketio.emit('withdraw_approved', w)
            elif action=='reject':
                w['status']='rejected'
                update_user_balance(w['user_id'],w['amount'],'withdraw rejected')
                socketio.emit('withdraw_rejected',w)
            save_json(WITHDRAWS_FILE,items)
            return jsonify({'ok':True})
    return jsonify({'ok':False})

# WebSocket events
@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

# Запуск
if __name__=='__main__':
    socketio.run(app, port=5000)
