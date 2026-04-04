import psycopg2
from dotenv import load_dotenv
import os


load_dotenv()

conn = psycopg2.connect(
    dbname=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    host=os.getenv("DB_HOST"),
    port=os.getenv("DB_PORT")
)

conn.autocommit = True
cur = conn.cursor()

def clear_all_rows():
    cur.execute("TRUNCATE TABLE cex_trades, dex_trades, portfolio_snapshots, account_snapshots RESTART IDENTITY CASCADE;")
    cur.close()
    conn.close()
    
    

if __name__ == "__main__":
    clear_all_rows()