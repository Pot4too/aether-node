#include <stdint.h>

#include "driver/gpio.h"
#include "driver/i2c.h"
#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define I2C_MASTER_PORT I2C_NUM_0
#define I2C_MASTER_SDA GPIO_NUM_4
#define I2C_MASTER_SCL GPIO_NUM_5
#define I2C_MASTER_FREQ_HZ 100000

#define HTU20D_I2C_ADDR 0x40
#define HTU20D_CMD_TEMP_NO_HOLD 0xF3

static const char *TAG = "htu20d";

static uint8_t htu20d_crc8(const uint8_t *data, size_t length)
{
    uint8_t crc = 0x00;

    for (size_t i = 0; i < length; ++i)
    {
        crc ^= data[i];
        for (int bit = 0; bit < 8; ++bit)
        {
            if (crc & 0x80)
            {
                crc = (uint8_t)((crc << 1) ^ 0x31);
            }
            else
            {
                crc <<= 1;
            }
        }
    }

    return crc;
}

static void i2c_master_init(void)
{
    i2c_config_t config = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = I2C_MASTER_SDA,
        .scl_io_num = I2C_MASTER_SCL,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = I2C_MASTER_FREQ_HZ,
        .clk_flags = 0,
    };

    ESP_ERROR_CHECK(i2c_param_config(I2C_MASTER_PORT, &config));
    ESP_ERROR_CHECK(i2c_driver_install(I2C_MASTER_PORT, config.mode, 0, 0, 0));
}

static esp_err_t htu20d_read_temperature(float *temperature_c)
{
    uint8_t data[3] = {0};
    i2c_cmd_handle_t command = i2c_cmd_link_create();
    esp_err_t err = ESP_OK;

    if (command == NULL)
    {
        return ESP_ERR_NO_MEM;
    }

    ESP_ERROR_CHECK(i2c_master_start(command));
    ESP_ERROR_CHECK(i2c_master_write_byte(command, (HTU20D_I2C_ADDR << 1) | I2C_MASTER_WRITE, true));
    ESP_ERROR_CHECK(i2c_master_write_byte(command, HTU20D_CMD_TEMP_NO_HOLD, true));
    ESP_ERROR_CHECK(i2c_master_stop(command));

    err = i2c_master_cmd_begin(I2C_MASTER_PORT, command, pdMS_TO_TICKS(1000));
    i2c_cmd_link_delete(command);
    if (err != ESP_OK)
    {
        return err;
    }

    vTaskDelay(pdMS_TO_TICKS(100));

    command = i2c_cmd_link_create();
    if (command == NULL)
    {
        return ESP_ERR_NO_MEM;
    }

    ESP_ERROR_CHECK(i2c_master_start(command));
    ESP_ERROR_CHECK(i2c_master_write_byte(command, (HTU20D_I2C_ADDR << 1) | I2C_MASTER_READ, true));
    ESP_ERROR_CHECK(i2c_master_read_byte(command, &data[0], I2C_MASTER_ACK));
    ESP_ERROR_CHECK(i2c_master_read_byte(command, &data[1], I2C_MASTER_ACK));
    ESP_ERROR_CHECK(i2c_master_read_byte(command, &data[2], I2C_MASTER_NACK));
    ESP_ERROR_CHECK(i2c_master_stop(command));

    err = i2c_master_cmd_begin(I2C_MASTER_PORT, command, pdMS_TO_TICKS(1000));
    i2c_cmd_link_delete(command);
    if (err != ESP_OK)
    {
        return err;
    }

    if (htu20d_crc8(data, 2) != data[2])
    {
        return ESP_ERR_INVALID_CRC;
    }

    uint16_t raw = ((uint16_t)data[0] << 8) | data[1];
    raw &= 0xFFFC;
    *temperature_c = -46.85f + (175.72f * (float)raw / 65536.0f);

    return ESP_OK;
}

void app_main(void)
{
    i2c_master_init();
    ESP_LOGI(TAG, "HTU20D temperature reader started on SDA=%d SCL=%d", I2C_MASTER_SDA, I2C_MASTER_SCL);

    while (1)
    {
        float temperature_c = 0.0f;
        esp_err_t err = htu20d_read_temperature(&temperature_c);

        if (err == ESP_OK)
        {
            ESP_LOGI(TAG, "Temperature: %.2f C", temperature_c);
        }
        else
        {
            ESP_LOGW(TAG, "HTU20D read failed: %s", esp_err_to_name(err));
        }

        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}