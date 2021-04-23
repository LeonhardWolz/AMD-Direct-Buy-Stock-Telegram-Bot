import datetime
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import yaml
from signal import SIGINT, signal

import requests
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.ext import CommandHandler, Filters, MessageHandler, Updater
from telegram.utils.request import Request

CREATE_USERS_TABLE = """CREATE TABLE IF NOT EXISTS connected_users (
    id integer PRIMARY KEY,
    name text NOT NULL);"""

CREATE_LISTED_PRODUCTS_TABLE = """CREATE TABLE IF NOT EXISTS listed_products (
    name text PRIMARY KEY,
    price text,
    product_page text,
    available integer);"""


class BotHandler(object):
    AMD_URL = "https://www.amd.com/de/direct-buy/de"
    HTTP_HEADERS = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.128 Safari/537.36"}

    INSERT_CONNECTED_USER = "INSERT INTO connected_users VALUES ({}, \"{}\");"
    REMOVE_CONNECTED_USER = "DELETE FROM connected_users WHERE id={};"

    INSERT_LISTED_PRODUCT = """INSERT INTO listed_products VALUES (\"{}\", \"{}\", \"{}\", {});"""
    REMOVE_LISTED_PRODUCT = """DELETE FROM listed_products WHERE name=\"{}\";"""
    UPDATE_LISTED_PRODUCT = """UPDATE listed_products SET price=\"{}\", product_page=\"{}\", available={} WHERE name=\"{}\";"""

    SCAN_INTERVAL = 30


    def __init__(self, db_conn, bot_token):
        self.db_conn = db_conn
        self.bot_token = bot_token
        
        self.bot_start()
    
    @property
    def last_stock(self):
        last_stock = {}
        for product in self.db_conn.cursor().execute("SELECT * FROM listed_products").fetchall():
            last_stock[product[0]] = (product[1], product[2], product[3])
        
        return last_stock


    def bot_start(self):
        bot = Bot(token=self.bot_token,
                    request=Request(con_pool_size=40))

        updater = Updater(bot=bot,
                            use_context=True,
                            workers=16)

        dispatcher = updater.dispatcher

        dispatcher.add_handler(CommandHandler("start", self.start))
        dispatcher.add_handler(CommandHandler("subscribe", self.subscribe))
        dispatcher.add_handler(CommandHandler("unsubscribe", self.unsubscribe))
        dispatcher.add_handler(CommandHandler("help", self.help))
        dispatcher.add_handler(CommandHandler("current", self.current))

        dispatcher.run_async(self.scan_sites, bot)

        updater.start_polling()

        updater.idle()

    def start(self, update, context):
        update.message.reply_text("Hello. I am the Amd Direct Buy stock notifier bot. I will provide notifications when "
                                f"the products listed at {self.AMD_URL} change. If you want to receive updates when the "
                                "available products change you can /subscribe and /unsubscribe. You can view all commands with /help.")

    def subscribe(self, update, context):
        if not self.db_conn.cursor().execute(f"SELECT * FROM connected_users WHERE id={update.message.from_user.id}").fetchall():
            try:
                statement = self.INSERT_CONNECTED_USER.format(update.message.from_user.id, update.message.from_user.first_name)
                self.db_conn.cursor().execute(statement)
                self.db_conn.commit()

                update.message.reply_text("Subscribed! You will now receive notifications when the available products change. "
                "You can unsubscribe at any time by using the /unsubscribe command.")
            except sqlite3.Error as e:
                update.message.reply_text("Error. Please try again.")
                print(e)
        else:
            update.message.reply_text("You are already subscribed! You will receive notifications when the available products change. "
            "You can unsubscribe at any time by using the /unsubscribe command.")


    def unsubscribe(self, update, context):
        try:
            statement = self.REMOVE_CONNECTED_USER.format(update.message.from_user.id)
            self.db_conn.cursor().execute(statement)
            self.db_conn.commit()

            update.message.reply_text("Unsubscribed! You will no longer receive notifications when the available products change. "
            "You can subscribe again at any time by using the /subscribe command.")
        except sqlite3.Error as e:
            update.message.reply_text("Error. Please try again.")
            print(e)

    def current(self, update, context):
        update.message.reply_text(self.get_currently_available(self.last_stock))

    def help(self, update, context):
        update.message.reply_text("You can issue the following commands:"
        "\n- /subscribe : Subscribe to updates"
        "\n- /unsubscribe : Unsubscribe from updates"
        "\n- /help : List all commands"
        "\n- /current : Get a list of currently available products")


    def bot_send_message(self, bot, message):
        try:
            connected_users = self.db_conn.cursor().execute("SELECT * FROM connected_users")
            for user in connected_users.fetchall():
                bot.sendMessage(chat_id=user[0], text=message)
        except sqlite3.Error as e:
            print("Error during sending messages", e)
    
    def get_currently_available(self, product_dict):
        available_str = f"Products currently available for purchase at {self.AMD_URL}:\n"
        for product_info in product_dict.items():
            if product_info[1][2]:
                available_str += f" - {product_info[0]}\n"
                available_str += f"    Product Page: {product_info[1][1]}\n"
                available_str += f"    Price: {product_info[1][0]}\n"
        
        available_str += "\nProducts listed but not available for purchase:\n"
        for product_info in product_dict.items():
            if not product_info[1][2]:
                available_str += f" - {product_info[0]}\n"
                available_str += f"    Product Page: {product_info[1][1]}\n"
                available_str += f"    Price: {product_info[1][0]}\n"
        
        return available_str

    def scan_sites(self, bot):

        while True:
            last_stock = self.last_stock

            page = requests.get(self.AMD_URL, headers=self.HTTP_HEADERS, timeout=5)
            soup = BeautifulSoup(page.content, "html.parser")

            shop_items = soup.find_all("div", class_="shop-content")

            current_stock = self.extract_current_stock(shop_items)

            # dict of new products
            new_products = {k: v for (k, v) in current_stock.items() if k not in last_stock}

            # dict of new and old products that are available
            new_available_products = {k: v for (k, v) in current_stock.items() if (k not in last_stock and v[2]) or (k in last_stock and v[2] > last_stock[k][2])}

            # dict of new and old products that are not available
            new_unavailable_products = {k: v for (k, v) in current_stock.items() if (k not in last_stock and not v[2]) or (k in last_stock and v[2] < last_stock[k][2])}

            # dict of products no longer listed on the page
            dropped_products = {k: v for (k, v) in last_stock.items() if k not in current_stock}

            # dict of products with updated price
            changed_price = {k: v for (k, v) in current_stock.items() if k in last_stock and v[0] != last_stock[k][0]}

            # send bot message and update db if site changed
            if new_products or new_available_products or new_unavailable_products or dropped_products or changed_price:
                self.bot_send_message(bot, self.generate_bot_message(new_products, bot_message, new_available_products, new_unavailable_products, dropped_products, changed_price, last_stock, current_stock))

                self.update_database(new_products, 
                                    dropped_products, 
                                    new_available_products, 
                                    new_unavailable_products, 
                                    changed_price)

            time.sleep(self.SCAN_INTERVAL)

    def extract_current_stock(self, shop_items):
        current_stock = {}
        for shop_item in shop_items:
            item_name = re.findall("(?!\s+).*(?=\n\s+)", shop_item.find("div", class_="shop-title").text)[0]

            price = re.findall("(?!\s+).*(?=\n\s+)", shop_item.find("div", class_="shop-price").text)[0]
            product_page = "https://www.amd.com" + (shop_item.find("div", class_="shop-full-specs-link").find("a", href=True)["href"])
            available = True if re.search("(Add to cart)", shop_item.find("div", class_="shop-links").text) else False

            current_stock[item_name] = (price, product_page, available)

        return current_stock

    def generate_bot_message(self, new_products, new_available_products, new_unavailable_products, dropped_products, changed_price, last_stock, current_stock):
        bot_message = ""
        if new_products:
            bot_message += "Products that were added to the store: \n"
            for product in new_products.items():
                bot_message += f" - {product[0]}\n"
            bot_message += "\n\n"

        if new_available_products:
            bot_message += "Products that became available for purchase: \n"
            for product in new_available_products.items():
                bot_message += f" - {product[0]}\n"
            bot_message += "\n\n"

        if new_unavailable_products:
            bot_message += "Products that are no longer available for purchase: \n"
            for product in new_unavailable_products.items():
                bot_message += f" - {product[0]}\n"
            bot_message += "\n\n"

        if dropped_products:
            bot_message += "Products that were removed from the store: \n"
            for product in dropped_products.items():
                bot_message += f" - {product[0]}\n"
            bot_message += "\n\n"

        if changed_price:
            bot_message += "Products that had their price updated: \n"
            for product in changed_price.items():
                bot_message += f" - {product[0]} Was: {last_stock[product[0]][0]} Now: {product[1][0]}\n"
            bot_message += "\n\n"

        bot_message += self.get_currently_available(current_stock)
        return bot_message
        
    def update_database(self, new_products, dropped_products, new_available_products, new_unavailable_products, changed_price):
        if new_products:
            try:
                for product in new_products.items():
                    statement = self.INSERT_LISTED_PRODUCT.format(product[0], product[1][0], product[1][1], product[1][2])
                    self.db_conn.cursor().execute(statement)
                self.db_conn.commit()
            except sqlite3.Error as e:
                print("Error during product entry into db", e)

        if dropped_products:
            try:
                for product in dropped_products.items():
                    statement = self.REMOVE_LISTED_PRODUCT.format(product[0])
                    self.db_conn.cursor().execute(statement)
                self.db_conn.commit()
            except sqlite3.Error as e:
                print("Error during product removal from db", e)
        
        if new_available_products or new_unavailable_products or changed_price:
            try:
                for product in {**{**new_available_products, **new_unavailable_products}, **changed_price}.items():
                    statement = self.UPDATE_LISTED_PRODUCT.format(product[1][0], product[1][1], product[1][2], product[0])
                    self.db_conn.cursor().execute(statement)
                self.db_conn.commit()

            except sqlite3.Error as e:
                print("Error during product update in db", e)

def get_connection():
    conn = None
    try:
        conn = sqlite3.connect(os.getcwd() + "\\bot_data.db", check_same_thread=False)
        cursor = conn.cursor()
        cursor.execute(CREATE_USERS_TABLE)
        cursor.execute(CREATE_LISTED_PRODUCTS_TABLE)
        return conn
    except sqlite3.Error as e:
        print("Error during DB creation: ", e)
        return None

def main():
    conn = get_connection()
    if conn:
        with open("token.yml", "r") as file:
            content = yaml.safe_load(file)
            bot_token = content["token"]
        BotHandler(conn, bot_token)

if __name__ == '__main__':
    main()
