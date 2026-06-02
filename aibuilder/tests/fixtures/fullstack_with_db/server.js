const express = require("express");
const { Pool } = require("pg");
const app = express();
const pool = new Pool();
app.get("/users", async (req, res) => {
  const { rows } = await pool.query("SELECT * FROM users");
  res.json(rows);
});
app.listen(3000);
