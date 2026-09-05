// asciinema-player, pinned to a full version so a republished bundle cannot change what runs.
// To move: bump VERSION, then `curl <file> | openssl dgst -sha384 -binary | openssl base64 -A`.
const VERSION = "3.17.0";

export const DEMO_CAST = {
  playerVersion: VERSION,
  scriptIntegrity: "sha384-s55nTYAdrPwGWmKKQ1lCnoB8H9LbqmsXsqqqPAHK2+T5h9IfI2dTXTDXJcZnySJD",
  styleIntegrity: "sha384-05cmIVRzN7mR7nmqajPpGPUPqJ5VyTAGHL1xJuiGWfhpWDp5hEfBk50kr21f3ILM",
};
