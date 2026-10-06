# Dataset

The challenge dataset (`conversations.parquet`, ~2,500 synthetic lead↔seller WhatsApp
conversations) is **not committed**. It is PII-shaped (CPF, e-mail, phone, plate and CEP
appear in free text), so it is treated as sensitive even though it is synthetic.

```bash
make dataset   # downloads it into dataset/ (git-ignored)
make eval      # replays every lead message through the extractor and scores it
```

Schema (from the challenge's data dictionary): `conversation_id`, `message_index`,
`timestamp`, `sender_role` (`lead`/`vendedor`), `sender_name`, `message_type`
(`text`/`image`/`audio`/`document`), `message_body`, `channel`, `conversation_outcome`
(`ganho`/`perdido`/`em_negociacao`/`sem_resposta`), `lead_idade_informada`,
`veiculo_texto`.

How it is used:
- **Evaluation labels**: `lead_idade_informada` and `veiculo_texto` score the extractor.
- **Intent coverage**: lead messages after the seller's price, which are objections,
  acceptances and declines, shaped the intent rules (e.g. "pode mandar sim").
- **Masking check**: every lead message is masked and re-scanned for raw PII.
- **Messy-data behaviour**: media placeholders (`[documento] CNH_frente.pdf`) and
  out-of-order timestamps informed the media handling and the choice to order by
  `message_index`, not by timestamp.
