import sys, threading
from datetime import date
from app.storage import TodoStore
n=int(sys.argv[1]); sys.setswitchinterval(1e-6)
store=TodoStore(); torn=[]; stop=False; target=[0]
def racer():
    while not stop:
        store.complete(target[0])
t=threading.Thread(target=racer,daemon=True); t.start()
for i in range(n):
    target[0]=store._next
    d=store.create("race",date(2000,1,1)).to_dict()
    if d["overdue"] == d["done"]: torn.append(d)
stop=True
print(len(torn),"torn in",n, torn[:1])
