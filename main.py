import os
import requests
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import Column, Integer, String, Float, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

app = FastAPI()

# Servir arquivos estáticos (interface HTML)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Configurações do Asaas (Apenas como recebedor de Pix)
ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "$aact_Ydac213...")  # Substitua ou use variável de ambiente
ASAAS_URL = "https://www.asaas.com/api/v3"

HEADERS = {
    "access_token": ASAAS_API_KEY,
    "Content-Type": "application/json"
}

# --- BANCO DE DADOS LOCAL AUTÔNOMO (RENDER) ---
DATABASE_URL = "sqlite:///./sistema_pontos.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Usuario(Base):
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True, index=True)
    cpf = Column(String, unique=True, index=True)
    pontos = Column(Float, default=0.0)


Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- SCHEMAS DE ENTRADA ---
class DepositoRequest(BaseModel):
    cpf: str
    valor: float


class CliqueRequest(BaseModel):
    cpf: str
    quantidade_cliques: int = 1


class SaqueRequest(BaseModel):
    cpf: str
    pontos: float
    chave_pix: str


# --- REGRA DE CONVERSÃO INTERNA (RENDER) ---
# 1.0 ponto = R$ 1,00 (Gerenciado puramente pelo servidor)
VALOR_POR_PONTO = 1.0


# --- ROTAS DA APLICAÇÃO ---

@app.get("/")
def home():
    return FileResponse("static/index.html")


@app.get("/api/saldo/{cpf}")
def consultar_saldo(cpf: str, db: Session = Depends(get_db)):
    usuario = db.query(Usuario).filter(Usuario.cpf == cpf).first()
    if not usuario:
        return {"cpf": cpf, "pontos": 0.0, "valor_em_reais": 0.0}

    valor_convertido = usuario.pontos * VALOR_POR_PONTO
    return {
        "cpf": usuario.cpf,
        "pontos": usuario.pontos,
        "valor_em_reais": valor_convertido
    }


@app.post("/api/deposito")
def criar_deposito(req: DepositoRequest, db: Session = Depends(get_db)):
    if req.valor < 5.0:
        raise HTTPException(status_code=400, detail="Valor mínimo para cobrança via Pix no Asaas é R$ 5,00.")

    usuario = db.query(Usuario).filter(Usuario.cpf == req.cpf).first()
    if not usuario:
        usuario = Usuario(cpf=req.cpf, pontos=0.0)
        db.add(usuario)
        db.commit()

    # Cria ou busca o cliente no Asaas apenas para emissão do Pix
    payload_cliente = {"name": f"Cliente {req.cpf}", "cpfCnpj": req.cpf}
    res_cli = requests.post(f"{ASAAS_URL}/customers", json=payload_cliente, headers=HEADERS)

    if res_cli.status_code in [200, 201]:
        customer_id = res_cli.json().get("id")
    else:
        res_search = requests.get(f"{ASAAS_URL}/customers?cpfCnpj={req.cpf}", headers=HEADERS)
        customer_id = res_search.json()["data"][0]["id"]

    payload_cob = {
        "customer": customer_id,
        "billingType": "PIX",
        "value": req.valor,
        "dueDate": "2026-12-31",
        "description": f"Deposito de R$ {req.valor} na plataforma"
    }
    res_cob = requests.post(f"{ASAAS_URL}/payments", json=payload_cob, headers=HEADERS)
    cob_data = res_cob.json()

    if res_cob.status_code not in [200, 201]:
        raise HTTPException(status_code=400, detail=f"Erro Asaas: {cob_data.get('errors')}")

    payment_id = cob_data.get("id")
    res_pix = requests.get(f"{ASAAS_URL}/payments/{payment_id}/pixQrCode", headers=HEADERS)
    pix_data = res_pix.json()

    return {
        "payment_id": payment_id,
        "copy_paste": pix_data.get("payload"),
        "qr_code_base64": pix_data.get("encodedImage")
    }


@app.post("/api/webhook/asaas")
async def webhook_asaas(request: Request, db: Session = Depends(get_db)):
    data = await request.json()

    if data.get("event") in ["PAYMENT_RECEIVED", "PAYMENT_CONFIRMED"]:
        payment = data.get("payment", {})
        valor = float(payment.get("value", 0))
        cpf = payment.get("cpfCnpj")

        pontos_creditados = valor / VALOR_POR_PONTO

        usuario = db.query(Usuario).filter(Usuario.cpf == cpf).first()
        if usuario:
            usuario.pontos += pontos_creditados
            db.commit()

    return {"status": "ok"}


@app.post("/api/clique")
def registrar_clique(req: CliqueRequest, db: Session = Depends(get_db)):
    """
    Registra os cliques do usuário, incrementa os pontos no banco da Render
    e atualiza a conversão autônoma em Reais (BRL).
    """
    usuario = db.query(Usuario).filter(Usuario.cpf == req.cpf).first()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuário não cadastrado. Faça um depósito inicial.")

    pontos_ganhos = req.quantidade_cliques * 1.0  # 1 clique = 1 ponto
    usuario.pontos += pontos_ganhos
    db.commit()

    valor_convertido = usuario.pontos * VALOR_POR_PONTO
    return {
        "status": "sucesso",
        "novos_pontos": usuario.pontos,
        "novo_valor_reais": valor_convertido
    }


@app.post("/api/saque")
def solicitar_saque(req: SaqueRequest, db: Session = Depends(get_db)):
    usuario = db.query(Usuario).filter(Usuario.cpf == req.cpf).first()

    if not usuario or usuario.pontos < req.pontos:
        raise HTTPException(status_code=400, detail="Pontos insuficientes na plataforma.")

    valor_em_dinheiro = req.pontos * VALOR_POR_PONTO
    usuario.pontos -= req.pontos
    db.commit()

    return {
        "status": "sucesso",
        "mensagem": f"Saque de {req.pontos} pontos (R$ {valor_em_dinheiro:.2f}) registrado no servidor!",
        "saldo_restante_pontos": usuario.pontos,
        "valor_restante_reais": usuario.pontos * VALOR_POR_PONTO
    }